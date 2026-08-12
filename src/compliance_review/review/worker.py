from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from compliance_review.domain.models import ControlSurfaceResult, ReviewResult, WorkItem
from compliance_review.repository import RepositorySandbox
from compliance_review.review.events import AppendOnlyEventLog
from compliance_review.review.models import ModelRequest, WorkerExecution
from compliance_review.review.provider import ModelProvider, tool_schemas
from compliance_review.review.tools import ScopedToolExecutor, serialize_tool_result


class TokenBudget:
    """Small deterministic accounting layer; one token is approximated as four chars."""

    def __init__(self, maximum: int) -> None:
        if maximum < 100:
            raise ValueError("token budget must be at least 100")
        self.maximum = maximum
        self.used = 0

    def charge_text(self, value: str | None) -> None:
        if not value:
            return
        self.charge(max(1, (len(value) + 3) // 4))

    def charge(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("token charge must not be negative")
        if self.used + amount > self.maximum:
            raise RuntimeError("token budget exceeded")
        self.used += amount


class ReviewWorker:
    """Run one Work Item with isolated context, tools and output path."""

    def __init__(
        self,
        run_id: str,
        work_item: WorkItem,
        agent_id: str,
        provider: ModelProvider,
        sandbox: RepositorySandbox,
        output_root: Path,
        event_log: AppendOnlyEventLog,
        token_budget: int = 4000,
    ) -> None:
        self.run_id = run_id
        self.work_item = work_item
        self.agent_id = agent_id
        self.provider = provider
        self.sandbox = sandbox
        self.output_path = self._output_path(output_root)
        self.event_log = event_log
        self.token_budget = token_budget

    def run(self) -> WorkerExecution:
        attempt_id = f"{self.run_id}.{self.work_item.work_item_id}.{uuid.uuid4().hex[:8]}"
        context_fingerprint = _context_fingerprint(self.run_id, self.work_item)
        self.event_log.append(
            "worker_started",
            {
                "run_id": self.run_id,
                "work_item_id": self.work_item.work_item_id,
                "agent_id": self.agent_id,
                "attempt_id": attempt_id,
                "context_fingerprint": context_fingerprint,
            },
        )
        budget = TokenBudget(self.token_budget)
        try:
            result, tool_rounds = self._review(attempt_id, budget)
            self._write_result(result)
            execution = WorkerExecution(
                work_item_id=self.work_item.work_item_id,
                attempt_id=attempt_id,
                agent_id=self.agent_id,
                execution_status="completed",
                result_path=self.output_path.as_posix(),
                result=result,
                tool_rounds=tool_rounds,
                tokens_used=budget.used,
                context_fingerprint=context_fingerprint,
            )
            self.event_log.append(
                "worker_completed",
                {
                    "run_id": self.run_id,
                    "work_item_id": self.work_item.work_item_id,
                    "agent_id": self.agent_id,
                    "attempt_id": attempt_id,
                    "result_path": self.output_path.as_posix(),
                    "tool_rounds": tool_rounds,
                    "tokens_used": budget.used,
                },
            )
            return execution
        except Exception as exc:  # worker failure becomes structured state, not scheduler crash
            error = str(exc)
            result = self._failure_result(attempt_id, error)
            self._write_result(result)
            execution = WorkerExecution(
                work_item_id=self.work_item.work_item_id,
                attempt_id=attempt_id,
                agent_id=self.agent_id,
                execution_status="failed",
                result_path=self.output_path.as_posix(),
                result=result,
                tokens_used=budget.used,
                context_fingerprint=context_fingerprint,
                error=error,
            )
            self.event_log.append(
                "worker_failed",
                {
                    "run_id": self.run_id,
                    "work_item_id": self.work_item.work_item_id,
                    "agent_id": self.agent_id,
                    "attempt_id": attempt_id,
                    "error": error,
                    "result_path": self.output_path.as_posix(),
                },
            )
            return execution

    def _review(self, attempt_id: str, budget: TokenBudget) -> tuple[ReviewResult, int]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Review only the assigned Work Item. Use read-only tools, cite observed "
                    "evidence, and return one JSON review_result.v1 object. Do not claim "
                    "runtime behavior from static evidence."
                ),
            },
            {"role": "user", "content": json.dumps(self.work_item.model_dump(), sort_keys=True)},
        ]
        for message in messages:
            budget.charge_text(str(message.get("content", "")))
        executor = ScopedToolExecutor(self.sandbox, self.work_item)
        tool_rounds = 0
        while tool_rounds <= self.work_item.max_tool_rounds:
            response = self.provider.complete(
                ModelRequest(
                    work_item=self.work_item,
                    attempt_id=attempt_id,
                    agent_id=self.agent_id,
                    messages=messages,
                    tools=tool_schemas(),
                    token_budget=budget.maximum - budget.used,
                )
            )
            budget.charge(response.input_tokens + response.output_tokens)
            budget.charge_text(response.content)
            if response.tool_calls:
                tool_rounds += 1
                if tool_rounds > self.work_item.max_tool_rounds:
                    raise RuntimeError("work item max_tool_rounds exceeded")
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
                for call in response.tool_calls:
                    result = executor.execute(call)
                    serialized = serialize_tool_result(result)
                    budget.charge_text(serialized)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.call_id, "content": serialized}
                    )
                continue
            if not response.content:
                raise RuntimeError("model provider returned neither content nor tool calls")
            return (
                _parse_review_result(response.content, self.work_item, attempt_id, self.agent_id),
                tool_rounds,
            )
        raise RuntimeError("work item review loop exceeded max_tool_rounds")

    def _output_path(self, output_root: Path) -> Path:
        work_item_path = Path(self.work_item.work_item_id)
        if (
            len(work_item_path.parts) != 1
            or work_item_path.name in {"", ".", ".."}
            or work_item_path.name != self.work_item.work_item_id
        ):
            raise ValueError("work_item_id contains unsafe path characters")
        return output_root / self.work_item.work_item_id / "review-result.json"

    def _write_result(self, result: ReviewResult) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output_path.with_suffix(".json.tmp")
        temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(self.output_path)

    def _failure_result(self, attempt_id: str, error: str) -> ReviewResult:
        rows = [
            ControlSurfaceResult(
                control_id=control_id,
                surface=self.work_item.surface,
                evidence_status="missing",
                recommended_control_status="indeterminate",
                gap_reasons=[error],
            )
            for control_id in self.work_item.control_ids
        ]
        return ReviewResult(
            contract="review_result.v1",
            work_item_id=self.work_item.work_item_id,
            attempt_id=attempt_id,
            execution_status="failed",
            rows=rows,
            agent_id=self.agent_id,
            errors=[error],
        )


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


def _context_fingerprint(run_id: str, work_item: WorkItem) -> str:
    payload = json.dumps(
        {"run_id": run_id, "work_item": work_item.model_dump()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
