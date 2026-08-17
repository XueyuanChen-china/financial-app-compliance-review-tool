from __future__ import annotations

import hashlib
import json
import operator
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Mapping, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from compliance_review.code_map import CodeMapProvider, GraphifyCodeMapProvider
from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import (
    ControlSurfaceResult,
    EvidenceAnchor,
    EvidenceStrength,
    ReviewerEvidenceStatus,
    ReviewResult,
    Surface,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.context import (
    AgentRound,
    CompressedReviewMemory,
    ContextBudgetExceeded,
    ReviewerContextManager,
    ReviewerContextState,
    ReviewerRuntimeConfig,
)
from compliance_review.review.events import AppendOnlyEventLog
from compliance_review.review.evidence import (
    file_content_revision,
    normalize_snippet,
    strongest_evidence_strength,
)
from compliance_review.review.models import (
    ModelRequest,
    ModelResponse,
    ReviewRunSummary,
    ScopedToolResult,
    ToolCall,
    WorkerAttempt,
    WorkerExecution,
)
from compliance_review.review.provider import ModelProvider, tool_schemas
from compliance_review.review.redaction import redact_sensitive_text
from compliance_review.review.reliability import (
    ClassifiedWorkerError,
    ModelTimeoutError,
    ToolTimeoutError,
    WorkItemTimeoutError,
    call_with_timeout,
    classify_error,
)
from compliance_review.review.result_parser import parse_review_result
from compliance_review.review.tools import ScopedToolExecutor, serialize_tool_result

ProviderFactory = Callable[[WorkItem], ModelProvider]
_DEFAULT_REVIEW_TOKEN_BUDGET = 64000


class ParentState(TypedDict, total=False):
    """Serializable parent graph state; Work Items are the fan-out unit."""

    run_id: str
    work_items: list[dict[str, Any]]
    output_root: str
    event_log_path: str
    max_concurrency: int
    token_budget: int
    executions: Annotated[list[dict[str, Any]], operator.add]
    attempts: Annotated[list[dict[str, Any]], operator.add]
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
    tool_calls_used: int
    read_paths: list[str]
    error: str
    execution: dict[str, Any]
    output_root: str
    attempt_number: int
    predecessor_attempt_id: Optional[str]
    started_at: str
    deadline_monotonic: float
    error_code: str
    retryable: bool


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
        token_budget: int = _DEFAULT_REVIEW_TOKEN_BUDGET,
        checkpointer: Any | None = None,
        code_map_providers: Mapping[Surface, CodeMapProvider] | None = None,
        collector_results: Mapping[str, CollectorResult] | None = None,
        context_config: ReviewerRuntimeConfig | None = None,
        max_attempts: int = 2,
        model_timeout_seconds: float = 30.0,
        tool_timeout_seconds: float = 5.0,
        attempt_timeout_seconds: float = 90.0,
    ) -> None:
        if (provider is None) == (provider_factory is None):
            raise ValueError("provide exactly one provider or provider_factory")
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if min(model_timeout_seconds, tool_timeout_seconds, attempt_timeout_seconds) <= 0:
            raise ValueError("runtime timeouts must be positive")
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
        self.max_attempts = max_attempts
        self.model_timeout_seconds = model_timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds
        self.attempt_timeout_seconds = attempt_timeout_seconds

    def run(
        self,
        manifest_run_id: str,
        work_items: list[WorkItem],
        sandboxes: Mapping[str, RepositorySandbox],
        output_root: Path,
        event_log_path: Path | None = None,
        thread_id: str | None = None,
        collector_results: Mapping[str, CollectorResult] | None = None,
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
        graph = self._build_graph(
            sandboxes,
            output_root,
            event_log,
            dict(collector_results) if collector_results is not None else self.collector_results,
        )
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
                "attempts": [],
            },
            config=config,
        )
        return ReviewRunSummary.model_validate(result["summary"])

    def _build_graph(
        self,
        sandboxes: Mapping[str, RepositorySandbox],
        output_root: Path,
        event_log: AppendOnlyEventLog,
        collector_results: Mapping[str, CollectorResult],
    ) -> Any:
        reviewer_graph = _build_reviewer_graph(
            provider=self.provider,
            provider_factory=self.provider_factory,
            sandboxes=sandboxes,
            output_root=output_root,
            event_log=event_log,
            code_map_providers=self.code_map_providers,
            collector_results=collector_results,
            context_config=self.context_config,
            model_timeout_seconds=self.model_timeout_seconds,
            tool_timeout_seconds=self.tool_timeout_seconds,
            attempt_timeout_seconds=self.attempt_timeout_seconds,
        )

        def reviewer_node(state: ParentState) -> dict[str, Any]:
            work_item = WorkItem.model_validate(state["work_item"])
            agent_index = state.get("agent_index", 1)
            agent_id = f"reviewer-{agent_index:03d}"
            fingerprint = _context_fingerprint(state["run_id"], work_item)
            resumed, history = _load_resume_execution(
                Path(state["output_root"]), work_item, fingerprint
            )
            if resumed is not None:
                event_log.append(
                    "worker_resume_skipped",
                    {
                        "run_id": state["run_id"],
                        "work_item_id": work_item.work_item_id,
                        "attempt_id": resumed.attempt_id,
                        "reason": "valid_completed_attempt_reused",
                    },
                )
                return {
                    "executions": [resumed.model_dump()],
                    "attempts": [item.model_dump() for item in history],
                }

            previous = history[-1] if history else None
            if previous is not None and previous.status == "running":
                interrupted = previous.model_copy(
                    update={
                        "status": "interrupted",
                        "finished_at": _now(),
                        "error_code": "interrupted",
                        "error_message": "Attempt had no terminal state when the run resumed.",
                        "retryable": True,
                    }
                )
                _write_attempt_metadata(Path(state["output_root"]), interrupted)
                event_log.append(
                    "worker_attempt_interrupted",
                    {
                        "run_id": state["run_id"],
                        "work_item_id": work_item.work_item_id,
                        "attempt_id": interrupted.attempt_id,
                    },
                )
                history[-1] = interrupted
                previous = interrupted

            if previous is not None and previous.status in {"failed", "interrupted"}:
                if not previous.retryable or previous.attempt_number >= self.max_attempts:
                    exhausted = _execution_from_attempt(
                        Path(state["output_root"]), work_item, previous, fingerprint
                    )
                    if exhausted is not None:
                        return {
                            "executions": [exhausted.model_dump()],
                            "attempts": [item.model_dump() for item in history],
                        }

            attempt_number = (previous.attempt_number + 1) if previous else 1
            predecessor = previous.attempt_id if previous else None
            # A fingerprint mismatch means this is a new review context. Keep the
            # old attempt history, but grant the new context its own bounded budget.
            attempt_limit = self.max_attempts
            fresh_context = previous is not None and previous.context_fingerprint != fingerprint
            if previous is not None and (previous.status == "completed" or fresh_context):
                attempt_limit = attempt_number + self.max_attempts - 1
            terminal: WorkerExecution | None = None
            for number in range(attempt_number, attempt_limit + 1):
                attempt_id = _new_attempt_id(state["run_id"], work_item.work_item_id, number)
                _write_attempt_metadata(
                    Path(state["output_root"]),
                    WorkerAttempt(
                        work_item_id=work_item.work_item_id,
                        attempt_id=attempt_id,
                        attempt_number=number,
                        status="running",
                        started_at=_now(),
                        retryable=False,
                        context_fingerprint=fingerprint,
                        predecessor_attempt_id=predecessor,
                    ),
                )
                try:
                    reviewer_state = reviewer_graph.invoke(
                        {
                            "run_id": state["run_id"],
                            "work_item": work_item.model_dump(),
                            "agent_id": agent_id,
                            "attempt_id": attempt_id,
                            "attempt_number": number,
                            "predecessor_attempt_id": predecessor,
                            "output_root": state["output_root"],
                            "token_budget": state.get("token_budget", self.token_budget),
                        },
                        config={"max_concurrency": 1},
                    )
                    terminal = WorkerExecution.model_validate(reviewer_state["execution"])
                except Exception as exc:
                    classification = classify_error(exc)
                    terminal = _runtime_failure_execution(
                        state["run_id"],
                        work_item,
                        agent_id,
                        attempt_id,
                        number,
                        fingerprint,
                        classification.error_code,
                        classification.retryable,
                        str(exc),
                        Path(state["output_root"]),
                    )
                history.extend([terminal.attempt] if terminal.attempt is not None else [])
                if terminal.execution_status == "completed":
                    break
                if not terminal.retryable or number >= attempt_limit:
                    break
                event_log.append(
                    "worker_retry_scheduled",
                    {
                        "run_id": state["run_id"],
                        "work_item_id": work_item.work_item_id,
                        "attempt_id": terminal.attempt_id,
                        "attempt_number": number,
                        "error_code": terminal.error_code,
                    },
                )
                predecessor = terminal.attempt_id
            assert terminal is not None
            return {
                "executions": [terminal.model_dump()],
                "attempts": [item.model_dump() for item in history],
            }

        def summarize(state: ParentState) -> dict[str, Any]:
            executions = [
                WorkerExecution.model_validate(item) for item in state.get("executions", [])
            ]
            executions.sort(key=lambda item: item.work_item_id)
            summary = ReviewRunSummary(
                run_id=state["run_id"],
                executions=executions,
                max_concurrency=self.max_concurrency,
                completed=sum(item.execution_status == "completed" for item in executions),
                failed=sum(item.execution_status == "failed" for item in executions),
                event_log_path=state["event_log_path"],
                attempts=[WorkerAttempt.model_validate(item) for item in state.get("attempts", [])],
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
                "token_budget": state.get("token_budget", _DEFAULT_REVIEW_TOKEN_BUDGET),
                "output_root": state["output_root"],
            },
        )
        for index, work_item in enumerate(work_items, start=1)
    ]


def _build_reviewer_graph(
    provider: ModelProvider | None,
    provider_factory: ProviderFactory | None,
    sandboxes: Mapping[str, RepositorySandbox],
    output_root: Path,
    event_log: AppendOnlyEventLog,
    code_map_providers: Mapping[Surface, CodeMapProvider],
    collector_results: Mapping[str, CollectorResult],
    context_config: ReviewerRuntimeConfig,
    model_timeout_seconds: float,
    tool_timeout_seconds: float,
    attempt_timeout_seconds: float,
) -> Any:
    context_manager = ReviewerContextManager(context_config)

    def resolve_provider(work_item: WorkItem) -> ModelProvider:
        selected = provider_factory(work_item) if provider_factory else provider
        if selected is None:
            raise ValueError("provider is not configured")
        return selected

    def initialize(state: ReviewerState) -> dict[str, Any]:
        work_item = WorkItem.model_validate(state["work_item"])
        attempt_id = state.get("attempt_id") or (
            f"{state['run_id']}.{work_item.work_item_id}.{uuid.uuid4().hex[:8]}"
        )
        fingerprint = _context_fingerprint(state["run_id"], work_item)
        started_at = _now()
        output_root = Path(state["output_root"])
        instructions = (
            "Review only the assigned Work Item. Use read-only tools, cite observed "
            "evidence, and return one JSON review_result.v1 object. Do not claim "
            "runtime behavior from static evidence. Evidence discovery order is: "
            "use relevant deterministic collector facts first; use code_map_query or "
            "code_map_path for symbols, call paths, or data flow; use narrow search_code "
            "only after that; then use targeted read_file calls to form Evidence Anchors. "
            "A simple exact collector fact or exact search may skip code map navigation. "
            "Avoid repository-wide searches, prefer multiple small reads, and stop once "
            "the available evidence supports PASS, FAIL, or INDETERMINATE. Read at most "
            "200 lines per call."
        )
        context = context_manager.create(work_item, instructions)
        messages = context_manager.render_messages(context)
        tokens = sum(_approx_tokens(str(message.get("content", ""))) for message in messages)
        event_log.append(
            "worker_attempt_started",
            {
                "run_id": state["run_id"],
                "work_item_id": work_item.work_item_id,
                "agent_id": state["agent_id"],
                "attempt_id": attempt_id,
                "attempt_number": state.get("attempt_number", 1),
                "context_fingerprint": fingerprint,
            },
        )
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
            "tool_calls_used": 0,
            "read_paths": [],
            "output_root": output_root.as_posix(),
            "attempt_number": state.get("attempt_number", 1),
            "predecessor_attempt_id": state.get("predecessor_attempt_id"),
            "started_at": started_at,
            "deadline_monotonic": time.monotonic() + attempt_timeout_seconds,
        }

    def _ensure_attempt_time(state: ReviewerState) -> float:
        remaining = state.get("deadline_monotonic", time.monotonic()) - time.monotonic()
        if remaining <= 0:
            raise WorkItemTimeoutError("work item deadline exceeded")
        return remaining

    def _capture_error(exc: BaseException) -> dict[str, Any]:
        classification = classify_error(exc)
        return {
            "error": redact_sensitive_text(str(exc)),
            "error_code": classification.error_code,
            "retryable": classification.retryable,
            "response": {},
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
                    state.get("token_budget", _DEFAULT_REVIEW_TOKEN_BUDGET)
                    - state.get("tokens_used", 0)
                    - compression_tokens
                )
                if remaining_for_compression < 100:
                    raise ContextBudgetExceeded("context_budget_exhausted")
                _ensure_attempt_time(state)
                compression_response = call_with_timeout(
                    lambda: resolve_provider(work_item).complete(
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
                    ),
                    min(model_timeout_seconds, _ensure_attempt_time(state)),
                    ModelTimeoutError("compression model call timed out"),
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
                state.get("token_budget", _DEFAULT_REVIEW_TOKEN_BUDGET)
                - state.get("tokens_used", 0)
                - compression_tokens
            )
            if remaining < 100:
                return {
                    "error": "review budget exhausted before model conclusion",
                    "error_code": "review_budget_exhausted",
                    "retryable": False,
                    "response": {},
                    "context": prepared_context.model_dump(),
                    "tokens_used": state.get("tokens_used", 0) + compression_tokens,
                }
            response = call_with_timeout(
                lambda: resolve_provider(work_item).complete(
                    ModelRequest(
                        work_item=work_item,
                        attempt_id=state["attempt_id"],
                        agent_id=state["agent_id"],
                        messages=prepared_messages,
                        tools=tool_schemas(),
                        token_budget=remaining,
                    )
                ),
                min(model_timeout_seconds, _ensure_attempt_time(state)),
                ModelTimeoutError("model call timed out"),
            )
            used = (
                state.get("tokens_used", 0)
                + compression_tokens
                + response.input_tokens
                + response.output_tokens
            )
            used += _approx_tokens(response.content)
            # A response that finishes the Work Item is still useful even when the
            # provider reports a higher-than-estimated cumulative token count. Do
            # not start another tool round once the budget is exhausted.
            if response.tool_calls and used > state.get(
                "token_budget", _DEFAULT_REVIEW_TOKEN_BUDGET
            ):
                return {
                    "error": "review budget exhausted before model conclusion",
                    "error_code": "review_budget_exhausted",
                    "retryable": False,
                    "response": {},
                    "context": prepared_context.model_dump(),
                    "messages": prepared_messages,
                    "tokens_used": used,
                }
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
                "error_code": "context_budget_exhausted",
                "retryable": False,
                "response": {},
                "context": state.get("context", {}),
            }
        except Exception as exc:
            return _capture_error(exc)

    def execute_tools(state: ReviewerState) -> dict[str, Any]:
        try:
            work_item = WorkItem.model_validate(state["work_item"])
            context = ReviewerContextState.model_validate(state["context"])
            tool_rounds = state.get("tool_rounds", 0) + 1
            if tool_rounds > work_item.max_tool_rounds:
                raise RuntimeError("work item max_tool_rounds exceeded")
            response = ModelResponse.model_validate(state["response"])
            sandbox = sandboxes.get(work_item.work_item_id) or sandboxes.get(work_item.surface)
            if sandbox is None:
                raise ValueError(
                    f"missing sandbox for work item {work_item.work_item_id} "
                    f"and surface {work_item.surface}"
                )
            executor = ScopedToolExecutor(
                sandbox,
                work_item,
                code_map_provider=code_map_providers.get(
                    work_item.surface, GraphifyCodeMapProvider(sandbox.root)
                ),
                collector_results=dict(collector_results),
                tool_calls_used=state.get("tool_calls_used", 0),
                read_paths=set(state.get("read_paths", [])),
                max_tool_calls=work_item.max_tool_rounds * 3,
            )
            messages = list(state.get("messages", []))
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, sort_keys=True),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            used = state.get("tokens_used", 0)
            tool_results: list[ScopedToolResult] = []
            for call in response.tool_calls:

                def execute_call(tool_call: ToolCall = call) -> ScopedToolResult:
                    return executor.execute(tool_call)

                tool_result = call_with_timeout(
                    execute_call,
                    min(tool_timeout_seconds, _ensure_attempt_time(state)),
                    ToolTimeoutError(f"tool execution timed out: {call.name}"),
                )
                if not tool_result.ok and tool_result.error_code == "path_escape":
                    raise ValueError("path escape security policy violation")
                if not tool_result.ok and tool_result.retryable:
                    raise ClassifiedWorkerError(
                        tool_result.error_code or "tool_retryable_failure",
                        tool_result.error or "retryable tool failure",
                        retryable=True,
                    )
                tool_results.append(tool_result)
                serialized = serialize_tool_result(tool_result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": serialized}
                )
                used += _approx_tokens(serialized)
            round_item = AgentRound(
                round_number=_next_round_number(context),
                model_response=response.model_dump(),
                tool_calls=[call.model_dump() for call in response.tool_calls],
                tool_results=[result.model_dump() for result in tool_results],
                estimated_tokens=sum(
                    _approx_tokens(serialize_tool_result(result)) for result in tool_results
                )
                + response.input_tokens
                + response.output_tokens
                + _approx_tokens(response.content),
            )
            context = context_manager.record_round(context, round_item)
            context = context_manager.add_evidence_anchors(
                context,
                _anchors_from_tool_results(work_item, response.tool_calls, tool_results, sandbox),
            )
            if used > state.get("token_budget", _DEFAULT_REVIEW_TOKEN_BUDGET):
                return {
                    "messages": messages,
                    "context": context.model_dump(),
                    "tool_rounds": tool_rounds,
                    "tokens_used": used,
                    "tool_calls_used": executor.tool_calls,
                    "read_paths": sorted(executor.read_paths),
                    "error": "review budget exhausted before model conclusion",
                    "error_code": "review_budget_exhausted",
                    "retryable": False,
                    "response": {},
                }
            return {
                "messages": messages,
                "context": context.model_dump(),
                "tool_rounds": tool_rounds,
                "tokens_used": used,
                "tool_calls_used": executor.tool_calls,
                "read_paths": sorted(executor.read_paths),
            }
        except ContextBudgetExceeded:
            return {
                "error": "context_budget_exhausted",
                "error_code": "context_budget_exhausted",
                "retryable": False,
                "response": {},
            }
        except Exception as exc:
            return _capture_error(exc)

    def route_after_model(state: ReviewerState) -> str:
        if state.get("error"):
            return "finalize"
        response = ModelResponse.model_validate(state.get("response", {}))
        return "execute_tools" if response.tool_calls else "finalize"

    def finalize(state: ReviewerState) -> dict[str, Any]:
        work_item = WorkItem.model_validate(state["work_item"])
        attempt_id = state["attempt_id"]
        error = state.get("error")
        error_code = state.get("error_code")
        retryable = state.get("retryable", False)
        finalization_tokens = 0
        try:
            if error:
                context = ReviewerContextState.model_validate(state["context"])
                if error_code in {"review_budget_exhausted", "context_budget_exhausted"}:
                    result = _bounded_inconclusive_result(
                        work_item,
                        attempt_id,
                        state["agent_id"],
                        context.evidence_ledger,
                        error,
                    )
                    error = None
                    error_code = None
                    retryable = False
                else:
                    raise RuntimeError(error)
            else:
                response = ModelResponse.model_validate(state.get("response", {}))
                if not response.content:
                    raise RuntimeError("model provider returned neither content nor tool calls")
                provider_for_finalization = resolve_provider(work_item)
                if getattr(provider_for_finalization, "supports_strict_finalization", False):
                    result, finalization_tokens = _finalize_terminal_result(
                        provider=provider_for_finalization,
                        work_item=work_item,
                        attempt_id=attempt_id,
                        agent_id=state["agent_id"],
                        candidate_content=response.content,
                        evidence_ledger=ReviewerContextState.model_validate(
                            state["context"]
                        ).evidence_ledger,
                        timeout_seconds=min(model_timeout_seconds, _ensure_attempt_time(state)),
                    )
                else:
                    result = _parse_review_result(
                        response.content, work_item, attempt_id, state["agent_id"]
                    )
                context = ReviewerContextState.model_validate(state["context"])
                result = _attach_evidence_ledger(result, context.evidence_ledger)
        except Exception as exc:
            classification = classify_error(exc)
            error = redact_sensitive_text(str(exc))
            error_code = error_code or classification.error_code
            retryable = classification.retryable
            result = _failure_result(work_item, attempt_id, state["agent_id"], error)
        work_item_path = Path(work_item.work_item_id)
        if (
            len(work_item_path.parts) != 1
            or work_item_path.name in {"", ".", ".."}
            or work_item_path.name != work_item.work_item_id
        ):
            raise ValueError("work_item_id contains unsafe path characters")
        status: Literal["completed", "failed"] = (
            "completed" if result.execution_status == "completed" else "failed"
        )
        attempt = WorkerAttempt(
            work_item_id=work_item.work_item_id,
            attempt_id=attempt_id,
            attempt_number=state.get("attempt_number", 1),
            status=status,
            started_at=state.get("started_at", _now()),
            finished_at=_now(),
            error_code=error_code,
            error_message=redact_sensitive_text(error) if error else None,
            retryable=retryable,
            context_fingerprint=state["context_fingerprint"],
            predecessor_attempt_id=state.get("predecessor_attempt_id"),
        )
        result_path = _write_attempt_artifacts(
            Path(state["output_root"]),
            result,
            attempt,
        )
        execution = WorkerExecution(
            work_item_id=work_item.work_item_id,
            attempt_id=attempt_id,
            agent_id=state["agent_id"],
            execution_status=status,
            result_path=result_path.as_posix(),
            result=result,
            tool_rounds=state.get("tool_rounds", 0),
            tokens_used=state.get("tokens_used", 0) + finalization_tokens,
            context_fingerprint=state["context_fingerprint"],
            error=redact_sensitive_text(error) if error else None,
            error_code=error_code,
            retryable=retryable,
            attempt_number=state.get("attempt_number", 1),
            predecessor_attempt_id=state.get("predecessor_attempt_id"),
            attempt=attempt.model_copy(update={"result_ref": result_path.as_posix()}),
        )
        event_log.append(
            "worker_attempt_completed" if status == "completed" else "worker_attempt_failed",
            {
                "run_id": state["run_id"],
                "work_item_id": work_item.work_item_id,
                "agent_id": state["agent_id"],
                "attempt_id": attempt_id,
                "attempt_number": state.get("attempt_number", 1),
                "result_path": result_path.as_posix(),
                "error_code": error_code,
                "retryable": retryable,
            },
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
                **({"error": redact_sensitive_text(error)} if error else {}),
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
    return parse_review_result(redact_sensitive_text(content), work_item, attempt_id, agent_id)


def _finalize_terminal_result(
    *,
    provider: ModelProvider,
    work_item: WorkItem,
    attempt_id: str,
    agent_id: str,
    candidate_content: str,
    evidence_ledger: list[EvidenceAnchor],
    timeout_seconds: float,
) -> tuple[ReviewResult, int]:
    """Request a strict terminal result without repeating the investigation.

    Some compatible relays still occasionally ignore a response schema. The two
    bounded retries are finalization-only; known legacy shapes remain a fallback
    for the original candidate, while arbitrary prose is never guessed.
    """
    ledger = [
        anchor.model_dump(mode="json", exclude={"exact_snippet"}) for anchor in evidence_ledger
    ]
    request_payload = json.dumps(
        {
            "work_item": work_item.model_dump(mode="json"),
            "runtime_identifiers": {
                "work_item_id": work_item.work_item_id,
                "attempt_id": attempt_id,
                "agent_id": agent_id,
            },
            "evidence_ledger": ledger,
            "candidate_assessment": candidate_content,
        },
        ensure_ascii=False,
    )
    tokens = 0
    for retry in range(3):
        try:
            response = call_with_timeout(
                lambda: provider.complete(
                    ModelRequest(
                        work_item=work_item,
                        attempt_id=attempt_id,
                        agent_id=agent_id,
                        request_kind="review_finalization",
                        token_budget=4_000,
                        tools=[],
                        response_schema=ReviewResult.model_json_schema(),
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Return one strict review_result.v1 JSON object. "
                                    "Cover the assigned controls and surface only. "
                                    "Copy runtime_identifiers.work_item_id, "
                                    "runtime_identifiers.attempt_id, and "
                                    "runtime_identifiers.agent_id exactly into the result. "
                                    "Cite only ledger anchor IDs. "
                                    "Never claim runtime proof from static evidence. "
                                    "Do not call tools or begin a new investigation."
                                ),
                            },
                            {"role": "user", "content": request_payload},
                        ],
                    )
                ),
                timeout_seconds,
                ModelTimeoutError("terminal review finalization timed out"),
            )
            tokens += (
                response.input_tokens + response.output_tokens + _approx_tokens(response.content)
            )
            if response.tool_calls or not response.content:
                continue
            return _parse_review_result(response.content, work_item, attempt_id, agent_id), tokens
        except (TypeError, ValueError, ModelTimeoutError):
            if retry == 2:
                break
    # The initial model answer can still be accepted only through the strict
    # local parser. Unknown text fails and becomes a retryable worker error.
    return _parse_review_result(candidate_content, work_item, attempt_id, agent_id), tokens


def _parse_compressed_memory(content: str, generation: int) -> CompressedReviewMemory:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = CompressedReviewMemory.model_validate(json.loads(text))
        return parsed.model_copy(update={"generation": generation})
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid compressed review memory: {exc}") from exc


def _next_round_number(context: ReviewerContextState) -> int:
    rounds = [*context.retired_rounds, *context.active_rounds]
    return max((round_item.round_number for round_item in rounds), default=0) + 1


def _anchors_from_tool_results(
    work_item: WorkItem,
    calls: list[Any],
    results: list[Any],
    sandbox: RepositorySandbox,
) -> list[EvidenceAnchor]:
    anchors: list[EvidenceAnchor] = []
    for call, result in zip(calls, results):
        if not result.ok:
            continue
        output = result.output
        references: list[dict[str, Any]] = []
        if call.name == "read_file":
            start_line = int(call.arguments.get("start_line", 1))
            actual_line_count = len(output.splitlines()) if isinstance(output, str) else 0
            references.append(
                {
                    "path": call.arguments.get("path"),
                    "start_line": start_line,
                    "end_line": start_line + max(actual_line_count, 1) - 1,
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
                if not isinstance(fact, dict):
                    continue
                for source_ref in fact.get("source_refs", []):
                    if isinstance(source_ref, dict):
                        references.append(
                            {
                                **source_ref,
                                "_fact_id": fact.get("fact_id"),
                                "_evidence_strength": fact.get("evidence_strength"),
                            }
                        )
        for reference in references:
            path = reference.get("path") or reference.get("source_path")
            symbol = reference.get("symbol")
            if not path and not symbol:
                continue
            exact_snippet = _reference_snippet(call.name, output, reference)
            normalized_hash = (
                hashlib.sha256(normalize_snippet(exact_snippet).encode("utf-8")).hexdigest()
                if exact_snippet
                else None
            )
            anchor_payload = json.dumps(
                {
                    "work_item_id": work_item.work_item_id,
                    "call_id": call.call_id,
                    "path": path,
                    "symbol": symbol,
                    "start_line": reference.get("start_line") or reference.get("source_line"),
                    "snippet_hash": normalized_hash,
                },
                sort_keys=True,
            )
            file_revision = None
            if path:
                try:
                    file_revision = file_content_revision(sandbox.read_bytes(str(path)))
                except (OSError, ValueError):
                    file_revision = None
            anchors.append(
                EvidenceAnchor(
                    anchor_id="anchor."
                    + hashlib.sha256(anchor_payload.encode("utf-8")).hexdigest()[:20],
                    control_ids=list(work_item.control_ids),
                    source_surface=work_item.surface,
                    source_tool=call.name,
                    path=path,
                    symbol=symbol,
                    start_line=(
                        reference.get("start_line")
                        or reference.get("line_number")
                        or reference.get("source_line")
                    ),
                    end_line=(reference.get("end_line") or reference.get("line_number")),
                    exact_snippet=exact_snippet,
                    normalized_snippet_hash=normalized_hash,
                    file_revision=file_revision,
                    evidence_strength=(
                        reference.get("_evidence_strength")
                        or _tool_evidence_strength(call.name, work_item.surface)
                    ),
                    fact_ids=([str(reference["_fact_id"])] if reference.get("_fact_id") else []),
                    summary=f"Observed bounded result from {call.name}.",
                )
            )
    return anchors


def _attach_evidence_ledger(result: ReviewResult, anchors: list[EvidenceAnchor]) -> ReviewResult:
    anchors_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    rows = []
    for row in result.rows:
        cited = [
            anchors_by_id[anchor_id] for anchor_id in row.anchor_ids if anchor_id in anchors_by_id
        ]
        fact_ids = sorted({fact_id for anchor in cited for fact_id in anchor.fact_ids})
        strongest = strongest_evidence_strength([anchor.evidence_strength for anchor in cited])
        rows.append(
            row.model_copy(
                update={
                    "fact_ids": sorted(set([*row.fact_ids, *fact_ids])),
                    "observed_evidence_strength": row.observed_evidence_strength or strongest,
                }
            )
        )
    return result.model_copy(update={"rows": rows, "anchors": anchors})


def _reference_snippet(tool_name: str, output: Any, reference: dict[str, Any]) -> str | None:
    if tool_name == "read_file" and isinstance(output, str):
        return output
    for key in ("line_text", "snippet", "text"):
        value = reference.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _tool_evidence_strength(tool_name: str, surface: Surface) -> EvidenceStrength:
    if tool_name in {"code_map_query", "code_map_path"}:
        return "behavioral_hint"
    if surface == "backend_api_doc":
        return "server_doc"
    return "static_proof"


def _failure_result(
    work_item: WorkItem, attempt_id: str, agent_id: str, error: str
) -> ReviewResult:
    error = redact_sensitive_text(error)
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


def _bounded_inconclusive_result(
    work_item: WorkItem,
    attempt_id: str,
    agent_id: str,
    anchors: list[EvidenceAnchor],
    reason: str,
) -> ReviewResult:
    """Finish safely when bounded investigation cannot request another model turn."""
    relevant_anchor_ids = [
        anchor.anchor_id
        for anchor in anchors
        if set(anchor.control_ids).intersection(work_item.control_ids)
    ]
    evidence_status: ReviewerEvidenceStatus = "partial" if relevant_anchor_ids else "missing"
    return ReviewResult(
        contract="review_result.v1",
        work_item_id=work_item.work_item_id,
        attempt_id=attempt_id,
        execution_status="completed",
        rows=[
            ControlSurfaceResult(
                control_id=control_id,
                surface=work_item.surface,
                evidence_status=evidence_status,
                recommended_control_status="indeterminate",
                anchor_ids=relevant_anchor_ids,
                gap_reasons=[reason],
                observations=[
                    "Bounded static investigation ended before a model conclusion; "
                    "manual follow-up is required."
                ],
            )
            for control_id in work_item.control_ids
        ],
        anchors=anchors,
        agent_id=agent_id,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_attempt_id(run_id: str, work_item_id: str, attempt_number: int) -> str:
    return f"{run_id}.{work_item_id}.attempt-{attempt_number:03d}-{uuid.uuid4().hex[:8]}"


def _work_item_dir(output_root: Path, work_item_id: str) -> Path:
    relative = Path(work_item_id)
    if len(relative.parts) != 1 or relative.name != work_item_id or work_item_id in {"", ".", ".."}:
        raise ValueError("work_item_id contains unsafe path characters")
    return output_root / work_item_id


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(redact_sensitive_text(payload), encoding="utf-8")
    temporary.replace(path)


def _write_attempt_metadata(output_root: Path, attempt: WorkerAttempt) -> None:
    item_dir = _work_item_dir(output_root, attempt.work_item_id)
    attempt_dir = item_dir / "attempts" / f"attempt-{attempt.attempt_number:03d}"
    _write_json_atomic(attempt_dir / "attempt.json", attempt.model_dump(mode="json"))
    _write_json_atomic(
        item_dir / "latest.json",
        {
            "work_item_id": attempt.work_item_id,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt.attempt_number,
            "status": attempt.status,
            "result_ref": attempt.result_ref,
            "context_fingerprint": attempt.context_fingerprint,
        },
    )


def _write_attempt_artifacts(
    output_root: Path, result: ReviewResult, attempt: WorkerAttempt
) -> Path:
    item_dir = _work_item_dir(output_root, attempt.work_item_id)
    attempt_dir = item_dir / "attempts" / f"attempt-{attempt.attempt_number:03d}"
    result_path = attempt_dir / "review-result.json"
    updated_attempt = attempt.model_copy(update={"result_ref": result_path.as_posix()})
    _write_json_atomic(result_path, result.model_dump(mode="json"))
    _write_attempt_metadata(output_root, updated_attempt)
    # Keep the old path as a compatibility pointer to the latest result. History
    # remains durable under attempts/<n>/ and is never represented by this file.
    _write_json_atomic(item_dir / "review-result.json", result.model_dump(mode="json"))
    return result_path


def _load_attempt_history(output_root: Path, work_item_id: str) -> list[WorkerAttempt]:
    item_dir = _work_item_dir(output_root, work_item_id)
    attempts_dir = item_dir / "attempts"
    if not attempts_dir.is_dir():
        return []
    history: list[WorkerAttempt] = []
    for path in sorted(attempts_dir.glob("attempt-*/attempt.json")):
        try:
            history.append(
                WorkerAttempt.model_validate(json.loads(path.read_text(encoding="utf-8")))
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(history, key=lambda item: item.attempt_number)


def _execution_from_attempt(
    output_root: Path,
    work_item: WorkItem,
    attempt: WorkerAttempt,
    fingerprint: str,
) -> WorkerExecution | None:
    if attempt.status not in {"failed", "interrupted"}:
        return None
    if attempt.context_fingerprint != fingerprint:
        return None
    try:
        result_path = _resolve_attempt_ref(output_root, attempt.result_ref)
        if result_path is None:
            raise FileNotFoundError("failed attempt has no result reference")
        result = ReviewResult.model_validate(json.loads(result_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        result = _failure_result(
            work_item,
            attempt.attempt_id,
            "recovered-worker",
            attempt.error_message or "persisted failed attempt has no valid result",
        )
        result_path = (
            _work_item_dir(output_root, work_item.work_item_id)
            / "attempts"
            / f"attempt-{attempt.attempt_number:03d}"
            / "recovered-failure.json"
        )
        _write_json_atomic(result_path, result.model_dump(mode="json"))
    if (
        result.work_item_id != work_item.work_item_id
        or result.attempt_id != attempt.attempt_id
        or result.execution_status != "failed"
    ):
        result = _failure_result(
            work_item,
            attempt.attempt_id,
            result.agent_id,
            "persisted failed attempt identity is invalid",
        )
        result_path = (
            _work_item_dir(output_root, work_item.work_item_id)
            / "attempts"
            / f"attempt-{attempt.attempt_number:03d}"
            / "recovered-failure.json"
        )
        _write_json_atomic(result_path, result.model_dump(mode="json"))
    persisted_attempt = attempt.model_copy(update={"result_ref": result_path.as_posix()})
    _write_attempt_metadata(output_root, persisted_attempt)
    return WorkerExecution(
        work_item_id=work_item.work_item_id,
        attempt_id=attempt.attempt_id,
        agent_id=result.agent_id,
        execution_status="failed",
        result_path=result_path.as_posix(),
        result=result,
        error=attempt.error_message,
        error_code=attempt.error_code,
        retryable=attempt.retryable,
        attempt_number=attempt.attempt_number,
        predecessor_attempt_id=attempt.predecessor_attempt_id,
        context_fingerprint=attempt.context_fingerprint,
        attempt=persisted_attempt,
    )


def _load_resume_execution(
    output_root: Path, work_item: WorkItem, fingerprint: str
) -> tuple[WorkerExecution | None, list[WorkerAttempt]]:
    history = _load_attempt_history(output_root, work_item.work_item_id)
    if not history:
        return None, history
    latest = history[-1]
    if latest.status != "completed" or not latest.result_ref:
        return None, history
    try:
        result_path = _resolve_attempt_ref(output_root, latest.result_ref)
        if result_path is None:
            return None, history
        result = ReviewResult.model_validate(json.loads(result_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return None, history
    if (
        result.execution_status != "completed"
        or result.work_item_id != work_item.work_item_id
        or result.attempt_id != latest.attempt_id
        or latest.context_fingerprint != fingerprint
    ):
        return None, history
    execution = WorkerExecution(
        work_item_id=work_item.work_item_id,
        attempt_id=latest.attempt_id,
        agent_id=result.agent_id,
        execution_status="completed",
        result_path=latest.result_ref,
        result=result,
        attempt_number=latest.attempt_number,
        context_fingerprint=latest.context_fingerprint,
        attempt=latest,
    )
    return execution, history


def _resolve_attempt_ref(output_root: Path, result_ref: str | None) -> Path | None:
    if not result_ref:
        return None
    candidate = Path(result_ref)
    if not candidate.is_absolute():
        candidate = output_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(output_root.resolve())
    except ValueError as exc:
        raise ValueError("persisted attempt result leaves output root") from exc
    return resolved


def _runtime_failure_execution(
    run_id: str,
    work_item: WorkItem,
    agent_id: str,
    attempt_id: str,
    attempt_number: int,
    fingerprint: str,
    error_code: str,
    retryable: bool,
    error: str,
    output_root: Path,
) -> WorkerExecution:
    safe_error = redact_sensitive_text(error)
    result = _failure_result(work_item, attempt_id, agent_id, safe_error)
    attempt = WorkerAttempt(
        work_item_id=work_item.work_item_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        status="failed",
        started_at=_now(),
        finished_at=_now(),
        error_code=error_code,
        error_message=safe_error,
        retryable=retryable,
        context_fingerprint=fingerprint,
    )
    result_path = _write_attempt_artifacts(output_root, result, attempt)
    return WorkerExecution(
        work_item_id=work_item.work_item_id,
        attempt_id=attempt_id,
        agent_id=agent_id,
        execution_status="failed",
        result_path=result_path.as_posix(),
        result=result,
        error=safe_error,
        error_code=error_code,
        retryable=retryable,
        attempt_number=attempt_number,
        context_fingerprint=fingerprint,
        attempt=attempt.model_copy(update={"result_ref": result_path.as_posix()}),
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
