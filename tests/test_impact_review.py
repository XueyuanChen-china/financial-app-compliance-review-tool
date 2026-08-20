from __future__ import annotations

import json
import threading
import time

from compliance_review.domain.models import (
    ChangedHunk,
    DiffFile,
    ImpactDecision,
    ImpactWorkItem,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.impact import ImpactRuntime, ImpactValidator
from compliance_review.review.models import ModelRequest, ModelResponse, ToolCall
from compliance_review.review.provider import StaticModelProvider


def _item() -> ImpactWorkItem:
    return ImpactWorkItem(
        impact_work_item_id="iwi.permission.contacts.android_native",
        coverage_unit_id="cu.permission.contacts.android_native",
        control_id="permission.contacts",
        surface="android_native",
        repository_ids=["android"],
        baseline_anchor_locations=["AndroidManifest.xml:8-9"],
        changed_files=[
            DiffFile(
                repo_id="android",
                path="AndroidManifest.xml",
                change_type="modify",
                old_hunks=[ChangedHunk(start_line=8, line_count=1)],
            )
        ],
    )


def _work_item() -> WorkItem:
    return WorkItem(
        work_item_id="wi.permission.contacts.android_native",
        module_id="permissions",
        repository_id="android",
        surface="android_native",
        control_ids=["permission.contacts"],
        coverage_unit_ids=["cu.permission.contacts.android_native"],
        allowed_roots=["."],
    )


def test_direct_anchor_overlap_overrides_unaffected() -> None:
    item = _item()
    decision = ImpactDecision(
        coverage_unit_id=item.coverage_unit_id,
        status="unaffected",
        reasons=["unrelated"],
        changed_file_refs=["AndroidManifest.xml"],
    )

    result = ImpactValidator().validate([item], {item.coverage_unit_id: decision})

    assert result.decisions[0].status == "affected"
    assert "baseline_anchor_hunk_overlap" in result.errors[0]


def test_invalid_agent_response_fails_closed(tmp_path) -> None:
    item = _item()
    provider = StaticModelProvider(lambda _: ModelResponse(content="not-json"))

    root = tmp_path / "android"
    root.mkdir()
    work_item = _work_item()
    decisions = ImpactRuntime(provider).run(
        [item],
        {item.coverage_unit_id: work_item},
        {work_item.work_item_id: RepositorySandbox(root)},
    )

    assert decisions[item.coverage_unit_id].status == "affected"
    assert decisions[item.coverage_unit_id].reasons == ["impact_response_invalid_schema"]


def test_valid_unaffected_requires_a_changed_file_reference(tmp_path) -> None:
    item = _item().model_copy(update={"baseline_anchor_locations": []})
    provider = StaticModelProvider(
        lambda _: ModelResponse(
            content=json.dumps(
                {
                    "coverage_unit_id": item.coverage_unit_id,
                    "status": "unaffected",
                    "reasons": ["Resource color is unrelated to permission declaration."],
                    "changed_file_refs": ["res/values/colors.xml"],
                }
            )
        )
    )

    root = tmp_path / "android"
    root.mkdir()
    work_item = _work_item()
    decisions = ImpactRuntime(provider).run(
        [item],
        {item.coverage_unit_id: work_item},
        {work_item.work_item_id: RepositorySandbox(root)},
    )
    result = ImpactValidator().validate([item], decisions)

    assert result.decisions[0].status == "unaffected"


def test_tool_failure_cannot_produce_unaffected(tmp_path) -> None:
    item = _item().model_copy(update={"baseline_anchor_locations": []})
    responses = iter(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(call_id="tool-1", name="read_file", arguments={"path": "missing"})
                ]
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "coverage_unit_id": item.coverage_unit_id,
                        "status": "unaffected",
                        "reasons": ["unrelated"],
                        "changed_file_refs": ["res/values/colors.xml"],
                    }
                )
            ),
        ]
    )
    provider = StaticModelProvider(lambda _: next(responses))
    root = tmp_path / "android"
    root.mkdir()

    decisions = ImpactRuntime(provider).run(
        [item],
        {item.coverage_unit_id: _work_item()},
        {_work_item().work_item_id: RepositorySandbox(root)},
    )

    assert decisions[item.coverage_unit_id].status == "affected"
    assert decisions[item.coverage_unit_id].reasons == ["impact_tool_failure"]


def test_impact_tool_loop_preserves_chat_completion_tool_message(tmp_path) -> None:
    item = _item().model_copy(update={"baseline_anchor_locations": []})
    requests: list[ModelRequest] = []
    responses = iter(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(call_id="tool-1", name="read_file", arguments={"path": "evidence.txt"})
                ]
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "coverage_unit_id": item.coverage_unit_id,
                        "status": "unaffected",
                        "reasons": ["The changed file does not affect this control."],
                        "changed_file_refs": ["res/values/colors.xml"],
                    }
                )
            ),
        ]
    )

    def respond(request: ModelRequest) -> ModelResponse:
        requests.append(request)
        return next(responses)

    root = tmp_path / "android"
    root.mkdir()
    (root / "evidence.txt").write_text("evidence\n", encoding="utf-8")
    work_item = _work_item()
    decisions = ImpactRuntime(StaticModelProvider(respond)).run(
        [item],
        {item.coverage_unit_id: work_item},
        {work_item.work_item_id: RepositorySandbox(root)},
    )

    assert decisions[item.coverage_unit_id].status == "unaffected"
    assert requests[1].messages[-2]["role"] == "assistant"
    assert requests[1].messages[-2]["tool_calls"][0]["id"] == "tool-1"
    assert requests[1].messages[-1]["role"] == "tool"


def test_impact_runtime_limits_parallel_workers_to_three(tmp_path) -> None:
    class SlowProvider:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.peak_active = 0

        def complete(self, request: ModelRequest) -> ModelResponse:
            with self.lock:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            try:
                time.sleep(0.03)
                coverage_unit_id = json.loads(request.messages[1]["content"])["coverage_unit_id"]
                return ModelResponse(
                    content=json.dumps(
                        {
                            "coverage_unit_id": coverage_unit_id,
                            "status": "affected",
                            "reasons": ["bounded concurrency test"],
                        }
                    )
                )
            finally:
                with self.lock:
                    self.active -= 1

    root = tmp_path / "android"
    root.mkdir()
    items = [
        _item().model_copy(
            update={
                "impact_work_item_id": f"iwi.permission.{index}",
                "coverage_unit_id": f"cu.permission.{index}.android_native",
                "baseline_anchor_locations": [],
            }
        )
        for index in range(4)
    ]
    work_items = {
        item.coverage_unit_id: _work_item().model_copy(
            update={
                "work_item_id": f"wi.permission.{index}",
                "coverage_unit_ids": [item.coverage_unit_id],
            }
        )
        for index, item in enumerate(items)
    }
    sandboxes = {
        work_item.work_item_id: RepositorySandbox(root) for work_item in work_items.values()
    }
    provider = SlowProvider()

    decisions = ImpactRuntime(provider, max_concurrency=99).run(items, work_items, sandboxes)

    assert set(decisions) == {item.coverage_unit_id for item in items}
    assert provider.peak_active == 3
