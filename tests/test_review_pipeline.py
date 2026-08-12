from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from compliance_review.config.loader import load_controls, load_profile
from compliance_review.domain.models import (
    ControlSurfaceResult,
    ReviewResult,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review import ReviewManifestBuilder, ReviewScheduler, StaticModelProvider
from compliance_review.review.models import ModelRequest, ModelResponse, ToolCall
from compliance_review.review.tools import ScopedToolExecutor

FIXTURES = Path(__file__).parent / "fixtures" / "day2"
PROJECT_ROOT = Path(__file__).parents[1]


def _work_item(work_item_id: str) -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id,
        module_id="data_privacy_and_permissions",
        surface="backend_code",
        control_ids=[work_item_id],
        allowed_roots=["backend"],
        max_tool_rounds=3,
        max_files_read=2,
        max_lines_per_read=20,
    )


def _review_json(request: ModelRequest, work_item: WorkItem, agent_id: str) -> str:
    request_attempt_id = request.attempt_id
    result = ReviewResult(
        contract="review_result.v1",
        work_item_id=work_item.work_item_id,
        attempt_id=request_attempt_id,
        execution_status="completed",
        rows=[
            ControlSurfaceResult(
                control_id=work_item.control_ids[0],
                surface=work_item.surface,
                evidence_status="partial",
                recommended_control_status="indeterminate",
                observations=[f"isolated:{work_item.work_item_id}"],
            )
        ],
        agent_id=agent_id,
    )
    return result.model_dump_json()


def test_manifest_builder_groups_applicable_controls_by_module_and_surface() -> None:
    profile = load_profile(PROJECT_ROOT / "examples/app-profile.yaml")
    controls = load_controls(PROJECT_ROOT / "examples/mvp-controls.yaml")

    manifest = ReviewManifestBuilder().build(profile, controls, run_id="run-day3")

    assert manifest.contract == "review_manifest.v1"
    assert manifest.default_max_concurrency == 3
    assert len(manifest.work_items) >= 3
    assert not manifest.excluded_controls
    assert all(item.allowed_roots for item in manifest.work_items)
    assert len({item.work_item_id for item in manifest.work_items}) == len(
        manifest.work_items
    )


def test_scheduler_runs_three_work_items_in_parallel_and_isolates_outputs(tmp_path: Path) -> None:
    items = [_work_item(f"wi.test.{index}") for index in range(3)]
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=2)

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal active, maximum_active
        work_item = request.work_item
        agent_id = request.agent_id
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        barrier.wait()
        time.sleep(0.02)
        with lock:
            active -= 1
        return ModelResponse(content=_review_json(request, work_item, agent_id))

    provider = StaticModelProvider(response_factory)
    scheduler = ReviewScheduler(provider=provider, max_concurrency=3, token_budget=2000)
    summary = scheduler.run(
        manifest_run_id="run-parallel",
        work_items=items,
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
        event_log_path=tmp_path / "events.jsonl",
    )

    assert summary.completed == 3
    assert summary.failed == 0
    assert maximum_active == 3
    result_paths = [Path(execution.result_path) for execution in summary.executions]
    assert len(set(result_paths)) == 3
    assert all(path.is_file() for path in result_paths)
    assert all(execution.context_fingerprint for execution in summary.executions)
    assert len({execution.context_fingerprint for execution in summary.executions}) == 3

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert sum(event["event_type"] == "worker_started" for event in events) == 3
    assert sum(event["event_type"] == "worker_completed" for event in events) == 3


def test_worker_tool_call_is_scoped_to_work_item_root() -> None:
    work_item = _work_item("wi.tool")
    executor = ScopedToolExecutor(RepositorySandbox(FIXTURES), work_item)

    allowed = executor.execute(
        ToolCall(
            call_id="call-1",
            name="read_file",
            arguments={"path": "backend/app.py", "start_line": 1, "line_count": 4},
        )
    )
    denied = executor.execute(
        ToolCall(
            call_id="call-2",
            name="read_file",
            arguments={"path": "frontend/package.json"},
        )
    )

    assert allowed.ok is True
    assert "loan/disburse" in allowed.output
    assert denied.ok is False
    assert "outside work item roots" in (denied.error or "")


def test_worker_can_complete_after_read_only_tool_call(tmp_path: Path) -> None:
    item = _work_item("wi.tool-run")
    calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="read-1",
                        name="read_file",
                        arguments={"path": "backend/app.py", "line_count": 3},
                    )
                ]
            )
        return ModelResponse(
            content=_review_json(request, request.work_item, request.agent_id)
        )

    event_path = tmp_path / "events.jsonl"
    execution = ReviewScheduler(
        provider=StaticModelProvider(response_factory), max_concurrency=1
    ).run(
        manifest_run_id="run-tool",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
        event_log_path=event_path,
    )

    assert execution.completed == 1
    assert execution.executions[0].tool_rounds == 1
    assert calls == 2
