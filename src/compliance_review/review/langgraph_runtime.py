from __future__ import annotations

import hashlib
import json
import operator
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Mapping, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from compliance_review.code_map import CodeMapProvider, GraphifyCodeMapProvider
from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import ControlSurfaceResult, ReviewResult, Surface, WorkItem
from compliance_review.repository import RepositorySandbox
from compliance_review.review.context import (
    AgentRound,
    CompressedReviewMemory,
    ContextBudgetExceeded,
    EvidenceAnchor,
    ReviewerContextManager,
    ReviewerContextState,
    ReviewerRuntimeConfig,
)
from compliance_review.review.events import AppendOnlyEventLog
from compliance_review.review.models import (
    ModelRequest,
    ModelResponse,
    ReviewRunSummary,
    WorkerExecution,
)
from compliance_review.review.provider import ModelProvider, tool_schemas
from compliance_review.review.tools import ScopedToolExecutor, serialize_tool_result

ProviderFactory = Callable[[WorkItem], ModelProvider]


class ParentState(TypedDict, total=False):
    """Serializable parent graph state; Work Items are the fan-out unit."""

    run_id: str
    work_items: list[dict[str, Any]]
    output_root: str
    event_log_path: str
    max_concurrency: int
    token_budget: int
    executions: Annotated[list[dict[str, Any]], operator.add]
    summary: dict[str, Any]
    # These fields are populated only in an isolated Send payload.
    work_item: dict[str, Any]
    agent_index: int


class ReviewerState(TypedDict, total=False):
    """State for one reviewer subgraph invocation."""

    run_id: str
    work_item: dict[str, Any]
    agent_id: str
    attempt_id: str
    context_fingerprint: str
    messages: list[dict[str, Any]]
    context: dict[str, Any]
    response: dict[str, Any]
    tool_rounds: int
    tokens_used: int
    token_budget: int
    error: str
    execution: dict[str, Any]


class LangGraphReviewRuntime:
    """Run the review workflow as a checkpointable LangGraph parent graph.

    The parent graph fans out one reviewer subgraph per Work Item and defers its
    final summary until every branch has returned. A caller may inject a durable
    LangGraph checkpointer; the default in-memory saver keeps tests lightweight.
    """

    def __init__(
        self,
        provider: ModelProvider | None = None,
        provider_factory: ProviderFactory | None = None,
        max_concurrency: int = 3,
        token_budget: int = 4000,
        checkpointer: Any | None = None,
        code_map_providers: Mapping[Surface, CodeMapProvider] | None = None,
        collector_results: Mapping[str, CollectorResult] | None = None,
        context_config: ReviewerRuntimeConfig | None = None,
    ) -> None:
        if (provider is None) == (provider_factory is None):
            raise ValueError("provide exactly one provider or provider_factory")
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self.provider = provider
        self.provider_factory = provider_factory
        self.max_concurrency = max_concurrency
        self.token_budget = token_budget
        self.context_config = context_config or ReviewerRuntimeConfig(
            max_concurrency=max_concurrency
        )
        self.checkpointer = checkpointer or InMemorySaver()
        self.code_map_providers = dict(code_map_providers or {})
        self.collector_results = dict(collector_results or {})

    def run(
        self,
        manifest_run_id: str,
        work_items: list[WorkItem],
        sandboxes: Mapping[Surface, RepositorySandbox],
        output_root: Path,
        event_log_path: Path | None = None,
        thread_id: str | None = None,
    ) -> ReviewRunSummary:
        output_root.mkdir(parents=True, exist_ok=True)
        log_path = event_log_path or output_root.parent / "worker-events.jsonl"
        event_log = AppendOnlyEventLog(log_path)
        event_log.append(
            "run_started",
            {
                "run_id": manifest_run_id,
                "work_item_count": len(work_items),
                "max_concurrency": self.max_concurrency,
                "runtime": "langgraph",
            },
        )
        graph = self._build_graph(sandboxes, output_root, event_log)
        config = {
            "configurable": {"thread_id": thread_id or f"review-{manifest_run_id}"},
            "max_concurrency": self.max_concurrency,
        }
        result = graph.invoke(
            {
                "run_id": manifest_run_id,
                "work_items": [item.model_dump() for item in work_items],
                "output_root": output_root.as_posix(),
                "event_log_path": log_path.as_posix(),
                "max_concurrency": self.max_concurrency,
                "token_budget": self.token_budget,
                "executions": [],
            },
            config=config,
        )
        return ReviewRunSummary.model_validate(result["summary"])

    def _build_graph(
        self,
        sandboxes: Mapping[Surface, RepositorySandbox],
        output_root: Path,
        event_log: AppendOnlyEventLog,
    ) -> Any:
        reviewer_graph = _build_reviewer_graph(
            provider=self.provider,
            provider_factory=self.provider_factory,
            sandboxes=sandboxes,
            output_root=output_root,
            event_log=event_log,
            code_map_providers=self.code_map_providers,
            collector_results=self.collector_results,
            context_config=self.context_config,
        )

        def reviewer_node(state: ParentState) -> dict[str, Any]:
            work_item = WorkItem.model_validate(state["work_item"])
            agent_index = state.get("agent_index", 1)
            reviewer_state = reviewer_graph.invoke(
                {
                    "run_id": state["run_id"],
                    "work_item": work_item.model_dump(),
                    "agent_id": f"reviewer-{agent_index:03d}",
                    "token_budget": state.get("token_budget", self.token_budget),
                },
                config={"max_concurrency": 1},
            )
            execution = reviewer_state["execution"]
            # The subgraph owns messages/tool results for this Work Item only.
            # Return the terminal execution and release the local context.
            del reviewer_state
            return {"executions": [execution]}

        def summarize(state: ParentState) -> dict[str, Any]:
            executions = [
                WorkerExecution.model_validate(item)
                for item in state.get("executions", [])
            ]
            executions.sort(key=lambda item: item.work_item_id)
            summary = ReviewRunSummary(
                run_id=state["run_id"],
                executions=executions,
                max_concurrency=self.max_concurrency,
                completed=sum(item.execution_status == "completed" for item in executions),
                failed=sum(item.execution_status == "failed" for item in executions),
                event_log_path=state["event_log_path"],
            )
            event_log.append(
                "run_completed",
                {
                    "run_id": state["run_id"],
                    "completed": summary.completed,
                    "failed": summary.failed,
                    "runtime": "langgraph",
                },
            )
            return {"summary": summary.model_dump()}

        builder = StateGraph(ParentState)
        builder.add_node("reviewer", reviewer_node)
        builder.add_node("summarize", summarize, defer=True)
        builder.add_conditional_edges(START, _fan_out)
        builder.add_edge("reviewer", "summarize")
        builder.add_edge("summarize", END)
        return builder.compile(checkpointer=self.checkpointer)


def _fan_out(state: ParentState) -> list[Send] | list[str]:
    work_items = state.get("work_items", [])
    if not work_items:
        return ["summarize"]
    return [
        Send(
            "reviewer",
            {
                "run_id": state["run_id"],
                # Do not pass a shared nested dict/list object into parallel branches.
                "work_item": deepcopy(work_item),
                "agent_index": index,
                "token_budget": state.get("token_budget", 4000),
            },
        )
        for index, work_item in enumerate(work_items, start=1)
    ]


def _build_reviewer_graph(
    provider: ModelProvider | None,
    provider_factory: ProviderFactory | None,
    sandboxes: Mapping[Surface, RepositorySandbox],
    output_root: Path,
    event_log: AppendOnlyEventLog,
    code_map_providers: Mapping[Surface, CodeMapProvider],
    collector_results: Mapping[str, CollectorResult],
    context_config: ReviewerRuntimeConfig,
) -> Any:
    context_manager = ReviewerContextManager(context_config)
    def resolve_provider(work_item: WorkItem) -> ModelProvider:
        selected = provider_factory(work_item) if provider_factory else provider
        if selected is None:
            raise ValueError("provider is not configured")
        return selected

    def initialize(state: ReviewerState) -> dict[str, Any]:
        work_item = WorkItem.model_validate(state["work_item"])
        attempt_id = f"{state['run_id']}.{work_item.work_item_id}.{uuid.uuid4().hex[:8]}"
        fingerprint = _context_fingerprint(state["run_id"], work_item)
        instructions = (
            "Review only the assigned Work Item. Use read-only tools, cite observed "
            "evidence, and return one JSON review_result.v1 object. Do not claim "
            "runtime behavior from static evidence."
        )
        context = context_manager.create(work_item, instructions)
        messages = context_manager.render_messages(context)
        tokens = sum(_approx_tokens(str(message.get("content", ""))) for message in messages)
        event_log.append(
            "worker_started",
            {
                "run_id": state["run_id"],
                "work_item_id": work_item.work_item_id,
                "agent_id": state["agent_id"],
                "attempt_id": attempt_id,
                "context_fingerprint": fingerprint,
                "runtime": "langgraph",
            },
        )
        return {
            "attempt_id": attempt_id,
            "context_fingerprint": fingerprint,
            "messages": messages,
            "context": context.model_dump(),
            "tool_rounds": 0,
            "tokens_used": tokens,
        }

    def call_model(state: ReviewerState) -> dict[str, Any]:
        try:
            work_item = WorkItem.model_validate(state["work_item"])
            context = ReviewerContextState.model_validate(state["context"])
            # Re-render from structured state so retired rounds never leak back into
            # the live model context through an old accumulated messages list.
            messages = context_manager.render_messages(context)
            compression_tokens = 0

            def compress(
                memory: CompressedReviewMemory | None,
                retired_rounds: list[AgentRound],
            ) -> CompressedReviewMemory:
                nonlocal compression_tokens
                payload = {
                    "compressed_memory": memory.model_dump() if memory else None,
                    "retired_rounds": [round_item.model_dump() for round_item in retired_rounds],
                }
                remaining_for_compression = (
                    state.get("token_budget", 4000)
                    - state.get("tokens_used", 0)
                    - compression_tokens
                )
                if remaining_for_compression < 100:
                    raise ContextBudgetExceeded("context_budget_exhausted")
                compression_response = resolve_provider(work_item).complete(
                    ModelRequest(
                        work_item=work_item,
                        attempt_id=state["attempt_id"],
                        agent_id=state["agent_id"],
                        request_kind="compression",
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Compress only the supplied retired reviewer rounds into "
                                    "the requested CompressedReviewMemory JSON object. "
                                    "Do not invent evidence and do not include immutable "
                                    "context, the evidence ledger, or active rounds."
                                ),
                            },
                            {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                        ],
                        tools=[],
                        token_budget=remaining_for_compression,
                    )
                )
                compression_tokens += (
                    compression_response.input_tokens
                    + compression_response.output_tokens
                    + _approx_tokens(compression_response.content)
                )
                if compression_response.tool_calls or not compression_response.content:
                    raise ValueError("compression response must be structured JSON content")
                return _parse_compressed_memory(
                    compression_response.content,
                    generation=(memory.generation + 1 if memory else 1),
                )

            prepared_context, prepared_messages = context_manager.prepare_for_model(
                context, messages, compress
            )
            remaining = (
                state.get("token_budget", 4000)
                - state.get("tokens_used", 0)
                - compression_tokens
            )
            if remaining < 100:
                raise RuntimeError("token budget exceeded before model call")
            response = resolve_provider(work_item).complete(
                ModelRequest(
                    work_item=work_item,
                    attempt_id=state["attempt_id"],
                    agent_id=state["agent_id"],
                    messages=prepared_messages,
                    tools=tool_schemas(),
                    token_budget=remaining,
                )
            )
            used = (
                state.get("tokens_used", 0)
                + compression_tokens
                + response.input_tokens
                + response.output_tokens
            )
            used += _approx_tokens(response.content)
            if used > state.get("token_budget", 4000):
                raise RuntimeError("token budget exceeded")
            updates: dict[str, Any] = {
                "context": prepared_context.model_dump(),
                "messages": prepared_messages,
                "response": response.model_dump(),
                "tokens_used": used,
            }
            if not response.tool_calls:
                round_item = AgentRound(
                    round_number=_next_round_number(prepared_context),
                    model_response=response.model_dump(),
                    estimated_tokens=response.input_tokens
                    + response.output_tokens
                    + _approx_tokens(response.content),
                )
                updates["context"] = context_manager.record_round(
                    prepared_context, round_item
                ).model_dump()
            return updates
        except ContextBudgetExceeded:
            return {
                "error": "context_budget_exhausted",
                "response": {},
                "context": state.get("context", {}),
            }
        except Exception as exc:
            return {"error": str(exc), "response": {}}

    def execute_tools(state: ReviewerState) -> dict[str, Any]:
        try:
            work_item = WorkItem.model_validate(state["work_item"])
            context = ReviewerContextState.model_validate(state["context"])
            tool_rounds = state.get("tool_rounds", 0) + 1
            if tool_rounds > work_item.max_tool_rounds:
                raise RuntimeError("work item max_tool_rounds exceeded")
            response = ModelResponse.model_validate(state["response"])
            sandbox = sandboxes.get(work_item.surface)
            if sandbox is None:
                raise ValueError(f"missing sandbox for surface: {work_item.surface}")
            executor = ScopedToolExecutor(
                sandbox,
                work_item,
                code_map_provider=code_map_providers.get(
                    work_item.surface, GraphifyCodeMapProvider(sandbox.root)
                ),
                collector_results=dict(collector_results),
            )
            messages = list(state.get("messages", []))
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            used = state.get("tokens_used", 0)
            tool_results = []
            for call in response.tool_calls:
                tool_result = executor.execute(call)
                tool_results.append(tool_result)
                serialized = serialize_tool_result(tool_result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": serialized}
                )
                used += _approx_tokens(serialized)
            if used > state.get("token_budget", 4000):
                raise RuntimeError("token budget exceeded after tool result")
            round_item = AgentRound(
                round_number=_next_round_number(context),
                model_response=response.model_dump(),
                tool_calls=[call.model_dump() for call in response.tool_calls],
                tool_results=[result.model_dump() for result in tool_results],
                estimated_tokens=sum(
                    _approx_tokens(serialize_tool_result(result))
                    for result in tool_results
                )
                + response.input_tokens
                + response.output_tokens
                + _approx_tokens(response.content),
            )
            context = context_manager.record_round(context, round_item)
            context = context_manager.add_evidence_anchors(
                context, _anchors_from_tool_results(work_item, response.tool_calls, tool_results)
            )
            return {
                "messages": messages,
                "context": context.model_dump(),
                "tool_rounds": tool_rounds,
                "tokens_used": used,
            }
        except ContextBudgetExceeded:
            return {"error": "context_budget_exhausted", "response": {}}
        except Exception as exc:
            return {"error": str(exc), "response": {}}

    def route_after_model(state: ReviewerState) -> str:
        if state.get("error"):
            return "finalize"
        response = ModelResponse.model_validate(state.get("response", {}))
        return "execute_tools" if response.tool_calls else "finalize"

    def finalize(state: ReviewerState) -> dict[str, Any]:
        work_item = WorkItem.model_validate(state["work_item"])
        attempt_id = state["attempt_id"]
        error = state.get("error")
        try:
            if error:
                raise RuntimeError(error)
            response = ModelResponse.model_validate(state.get("response", {}))
            if not response.content:
                raise RuntimeError("model provider returned neither content nor tool calls")
            result = _parse_review_result(
                response.content, work_item, attempt_id, state["agent_id"]
            )
        except Exception as exc:
            error = str(exc)
            result = _failure_result(work_item, attempt_id, state["agent_id"], error)
        result_path = output_root / work_item.work_item_id / "review-result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = result_path.with_suffix(".json.tmp")
        temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(result_path)
        status: Literal["completed", "failed"] = (
            "completed" if result.execution_status == "completed" else "failed"
        )
        execution = WorkerExecution(
            work_item_id=work_item.work_item_id,
            attempt_id=attempt_id,
            agent_id=state["agent_id"],
            execution_status=status,
            result_path=result_path.as_posix(),
            result=result,
            tool_rounds=state.get("tool_rounds", 0),
            tokens_used=state.get("tokens_used", 0),
            context_fingerprint=state["context_fingerprint"],
            error=error,
        )
        event_log.append(
            "worker_completed" if status == "completed" else "worker_failed",
            {
                "run_id": state["run_id"],
                "work_item_id": work_item.work_item_id,
                "agent_id": state["agent_id"],
                "attempt_id": attempt_id,
                "result_path": result_path.as_posix(),
                "tool_rounds": state.get("tool_rounds", 0),
                "tokens_used": state.get("tokens_used", 0),
                **({"error": error} if error else {}),
                "runtime": "langgraph",
            },
        )
        return {"execution": execution.model_dump()}

    builder = StateGraph(ReviewerState)
    builder.add_node("initialize", initialize)
    builder.add_node("call_model", call_model)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "call_model")
    builder.add_conditional_edges("call_model", route_after_model)
    builder.add_edge("execute_tools", "call_model")
    builder.add_edge("finalize", END)
    return builder.compile()


def _parse_review_result(
    content: str, work_item: WorkItem, attempt_id: str, agent_id: str
) -> ReviewResult:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        result = ReviewResult.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid structured review result: {exc}") from exc
    if result.work_item_id != work_item.work_item_id:
        raise RuntimeError("review result work_item_id does not match assigned work item")
    if result.attempt_id != attempt_id:
        raise RuntimeError("review result attempt_id does not match current attempt")
    if result.agent_id != agent_id:
        raise RuntimeError("review result agent_id does not match current worker")
    expected = {(control_id, work_item.surface) for control_id in work_item.control_ids}
    actual = {(row.control_id, row.surface) for row in result.rows}
    if not expected.issubset(actual):
        raise RuntimeError("review result does not cover every assigned control")
    return result


def _parse_compressed_memory(content: str, generation: int) -> CompressedReviewMemory:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return CompressedReviewMemory.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid compressed review memory: {exc}") from exc


def _next_round_number(context: ReviewerContextState) -> int:
    rounds = [*context.retired_rounds, *context.active_rounds]
    return max((round_item.round_number for round_item in rounds), default=0) + 1


def _anchors_from_tool_results(
    work_item: WorkItem, calls: list[Any], results: list[Any]
) -> list[EvidenceAnchor]:
    anchors: list[EvidenceAnchor] = []
    for call, result in zip(calls, results):
        if not result.ok:
            continue
        output = result.output
        references: list[dict[str, Any]] = []
        if call.name == "read_file":
            references.append(
                {
                    "path": call.arguments.get("path"),
                    "start_line": call.arguments.get("start_line"),
                    "end_line": (
                        int(call.arguments.get("start_line", 1))
                        + int(call.arguments.get("line_count", 1))
                        - 1
                    ),
                }
            )
        elif call.name in {"code_map_query", "code_map_path"} and isinstance(output, dict):
            entries = output.get("candidates", []) + output.get("nodes", [])
            references.extend(entry for entry in entries if isinstance(entry, dict))
            references.extend(
                entry
                for entry in output.get("relations", [])
                if isinstance(entry, dict) and entry.get("source_path")
            )
        elif call.name == "search_code" and isinstance(output, list):
            references.extend(entry for entry in output if isinstance(entry, dict))
        elif call.name == "get_collector_facts" and isinstance(output, dict):
            for fact in output.get("facts", []):
                for source_ref in fact.get("source_refs", []):
                    if isinstance(source_ref, dict):
                        references.append(source_ref)
        for reference in references:
            path = reference.get("path") or reference.get("source_path")
            symbol = reference.get("symbol")
            if not path and not symbol:
                continue
            anchors.append(
                EvidenceAnchor(
                    control_ids=list(work_item.control_ids),
                    source_tool=call.name,
                    path=path,
                    symbol=symbol,
                    start_line=reference.get("start_line") or reference.get("source_line"),
                    end_line=reference.get("end_line"),
                    summary=f"Observed bounded result from {call.name}.",
                )
            )
    return anchors


def _failure_result(
    work_item: WorkItem, attempt_id: str, agent_id: str, error: str
) -> ReviewResult:
    return ReviewResult(
        contract="review_result.v1",
        work_item_id=work_item.work_item_id,
        attempt_id=attempt_id,
        execution_status="failed",
        rows=[
            ControlSurfaceResult(
                control_id=control_id,
                surface=work_item.surface,
                evidence_status="missing",
                recommended_control_status="indeterminate",
                gap_reasons=[error],
            )
            for control_id in work_item.control_ids
        ],
        agent_id=agent_id,
        errors=[error],
    )


def _context_fingerprint(run_id: str, work_item: WorkItem) -> str:
    payload = json.dumps(
        {"run_id": run_id, "work_item": work_item.model_dump()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _approx_tokens(value: str | None) -> int:
    return max(1, (len(value) + 3) // 4) if value else 0
