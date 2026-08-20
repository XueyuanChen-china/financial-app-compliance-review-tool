from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Mapping, Sequence

from compliance_review.domain.models import (
    CoverageImpact,
    ImpactDecision,
    ImpactValidationResult,
    ImpactWorkItem,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.models import ModelRequest
from compliance_review.review.provider import ModelProvider, tool_schemas
from compliance_review.review.tools import ScopedToolExecutor, serialize_tool_result


class ImpactRuntime:
    """Bounded, binary impact stage run independently from Reviewer workers."""

    def __init__(self, provider: ModelProvider | None, max_concurrency: int = 3) -> None:
        self.provider = provider
        self.max_concurrency = max(1, min(max_concurrency, 3))

    def run(
        self,
        items: Sequence[ImpactWorkItem],
        work_items: Mapping[str, WorkItem],
        sandboxes: Mapping[str, RepositorySandbox],
    ) -> dict[str, ImpactDecision]:
        if self.provider is None:
            return {
                item.coverage_unit_id: _affected(item, "impact_provider_unavailable")
                for item in items
            }
        decisions: dict[str, ImpactDecision] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(items) or 1)) as pool:
            futures = {
                pool.submit(
                    self._run_one,
                    item,
                    work_items[item.coverage_unit_id],
                    sandboxes.get(work_items[item.coverage_unit_id].work_item_id),
                ): item
                for item in items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    decisions[item.coverage_unit_id] = future.result()
                except Exception as exc:  # Fail closed across provider/runtime failures.
                    decisions[item.coverage_unit_id] = _affected(
                        item, f"impact_worker_error:{type(exc).__name__}"
                    )
        return decisions

    def _run_one(
        self,
        item: ImpactWorkItem,
        work_item: WorkItem,
        sandbox: RepositorySandbox | None,
    ) -> ImpactDecision:
        if self.provider is None:
            return _affected(item, "impact_provider_unavailable")
        if sandbox is None:
            return _affected(item, "impact_sandbox_missing")
        messages: list[dict[str, object]] = [
            {
                "role": "system",
                "content": (
                    "You are an impact classifier, not a compliance reviewer. "
                    "Use Graphify/search/read only for navigation. Return affected or "
                    "unaffected only. Unaffected requires concrete changed-file references "
                    "and a Control-specific explanation."
                ),
            },
            {"role": "user", "content": item.model_dump_json()},
        ]
        executor = ScopedToolExecutor(sandbox, work_item, max_tool_calls=item.max_tool_rounds * 3)
        tool_failed = False
        for round_number in range(item.max_tool_rounds + 1):
            request = ModelRequest(
                work_item=work_item,
                attempt_id=f"impact.{item.impact_work_item_id}",
                agent_id="impact-agent",
                request_kind="impact",
                messages=messages,
                tools=tool_schemas(),
                response_schema=ImpactDecision.model_json_schema(),
                token_budget=2000,
            )
            response = self.provider.complete(request)
            if response.tool_calls:
                if round_number >= item.max_tool_rounds:
                    return _affected(item, "impact_tool_budget_exhausted")
                # Chat Completions requires the assistant tool-call turn before
                # the corresponding tool responses. Keeping this identical to
                # the Reviewer loop lets Impact safely use the same provider.
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
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
                for call in response.tool_calls:
                    result = executor.execute(call)
                    tool_failed = tool_failed or not result.ok
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": serialize_tool_result(result),
                        }
                    )
                continue
            if not response.content:
                return _affected(item, "impact_response_missing_terminal_decision")
            try:
                decision = ImpactDecision.model_validate(json.loads(response.content))
            except (TypeError, ValueError, json.JSONDecodeError):
                return _affected(item, "impact_response_invalid_schema")
            if decision.coverage_unit_id != item.coverage_unit_id:
                return _affected(item, "impact_response_unit_mismatch")
            if tool_failed and decision.status == "unaffected":
                return _affected(item, "impact_tool_failure")
            return decision
        return _affected(item, "impact_response_missing_terminal_decision")


class ImpactValidator:
    """Make Impact asymmetric: invalid or uncertain output always means affected."""

    def validate(
        self,
        items: Sequence[ImpactWorkItem],
        decisions: Mapping[str, ImpactDecision],
    ) -> ImpactValidationResult:
        errors: list[str] = []
        validated: list[ImpactDecision] = []
        for item in items:
            decision = decisions.get(item.coverage_unit_id)
            if decision is None:
                errors.append(f"{item.coverage_unit_id}:impact_decision_missing")
                validated.append(_affected(item, "impact_decision_missing"))
                continue
            if decision.status == "unaffected" and not decision.changed_file_refs:
                errors.append(f"{item.coverage_unit_id}:unaffected_without_changed_file_ref")
                validated.append(_affected(item, "unaffected_without_changed_file_ref"))
                continue
            if _direct_anchor_overlap(item):
                errors.append(f"{item.coverage_unit_id}:baseline_anchor_hunk_overlap")
                validated.append(_affected(item, "baseline_anchor_hunk_overlap"))
                continue
            validated.append(decision)
        return ImpactValidationResult(decisions=validated, errors=errors)


def impact_to_coverage(decision: ImpactDecision, item: ImpactWorkItem) -> CoverageImpact:
    return CoverageImpact(
        coverage_unit_id=item.coverage_unit_id,
        affected=decision.status == "affected",
        decision=decision.status,
        reasons=decision.reasons,
        repository_ids=item.repository_ids,
        changed_file_refs=decision.changed_file_refs,
        changed_hunk_refs=decision.changed_hunk_refs,
    )


def _affected(item: ImpactWorkItem, reason: str) -> ImpactDecision:
    return ImpactDecision(
        coverage_unit_id=item.coverage_unit_id,
        status="affected",
        reasons=[reason],
    )


def _direct_anchor_overlap(item: ImpactWorkItem) -> bool:
    for location in item.baseline_anchor_locations:
        path, start, end = _parse_location(location)
        if path is None or start is None or end is None:
            continue
        for changed in item.changed_files:
            if changed.previous_path not in {None, path} and changed.path != path:
                continue
            if changed.previous_path != path and changed.path != path:
                continue
            if any(
                _ranges_overlap(start, end, hunk.start_line, hunk.end_line)
                for hunk in changed.old_hunks
            ):
                return True
    return False


def _parse_location(location: str) -> tuple[str | None, int | None, int | None]:
    parts = location.rsplit(":", 1)
    if len(parts) != 2 or "-" not in parts[1]:
        return None, None, None
    try:
        start, end = (int(value) for value in parts[1].split("-", 1))
    except ValueError:
        return None, None, None
    return parts[0].split(":", 1)[-1], start, end


def _ranges_overlap(first_start: int, first_end: int, second_start: int, second_end: int) -> bool:
    return first_start <= second_end and second_start <= first_end
