from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from compliance_review.code_map import CodeMapCandidate, CodeMapQueryResult, CodeMapRelation
from compliance_review.collectors.base import CollectorResult
from compliance_review.config.loader import load_controls, load_profile
from compliance_review.domain.models import (
    ControlSurfaceResult,
    EvidenceAnchor,
    Fact,
    ReviewResult,
    SourceRef,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review import (
    LangGraphReviewRuntime,
    ReviewerRuntimeConfig,
    ReviewManifestBuilder,
    ReviewScheduler,
    StaticModelProvider,
)
from compliance_review.review.evidence import file_content_revision
from compliance_review.review.langgraph_runtime import (
    _anchors_from_tool_results,
    _parse_compressed_memory,
)
from compliance_review.review.models import (
    ModelRequest,
    ModelResponse,
    ScopedToolResult,
    ToolCall,
    WorkerAttempt,
)
from compliance_review.review.provider import tool_schemas
from compliance_review.review.result_parser import parse_review_result
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


def test_scoped_search_code_accepts_empty_root_as_all_allowed_roots() -> None:
    executor = ScopedToolExecutor(
        RepositorySandbox(FIXTURES),
        _work_item("search-empty-root"),
    )

    result = executor.execute(
        ToolCall(
            call_id="call-search-empty-root",
            name="search_code",
            arguments={"query": "/api/loan", "root": "", "file_globs": ["*.py"]},
        )
    )

    assert result.ok is True
    assert result.output == [
        {
            "path": "backend/app.py",
            "line_number": 2,
            "line_text": "@app.post('/api/loan/disburse')",
        }
    ]


def test_review_result_parser_accepts_wrapped_and_single_control_shapes() -> None:
    work_item = _work_item("parser-shape")
    wrapped = json.dumps(
        {
            "review_result.v1": {
                "contract": "review_result.v1",
                "work_item_id": work_item.work_item_id,
                "attempt_id": "attempt-1",
                "execution_status": "completed",
                "rows": [
                    {
                        "control_id": work_item.control_ids[0],
                        "surface": work_item.surface,
                        "evidence_status": "partial",
                        "recommended_control_status": "indeterminate",
                    }
                ],
                "agent_id": "reviewer-1",
            }
        }
    )
    wrapped_result = parse_review_result(wrapped, work_item, "attempt-1", "reviewer-1")
    assert wrapped_result.rows[0].control_id == work_item.control_ids[0]

    flat = json.dumps(
        {
            "review_result_version": "review_result.v1",
            "control_id": work_item.control_ids[0],
            "surface": work_item.surface,
            "status": "insufficient_evidence",
            "assessment": "The repository does not prove the requirement.",
            "limitations": ["No runtime evidence"],
        }
    )
    flat_result = parse_review_result(flat, work_item, "attempt-2", "reviewer-2")
    assert flat_result.rows[0].recommended_control_status == "indeterminate"
    assert flat_result.rows[0].evidence_status == "missing"

    implicit_assignment = json.dumps(
        {
            "review_result_version": "review_result.v1",
            "status": "fail",
            "summary": "A finding was observed.",
        }
    )
    implicit_result = parse_review_result(implicit_assignment, work_item, "attempt-3", "reviewer-3")
    assert implicit_result.rows[0].recommended_control_status == "fail"


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


def test_compressed_memory_generation_is_runtime_controlled() -> None:
    parsed = _parse_compressed_memory(
        json.dumps({"generation": 99, "findings": ["retained finding"]}),
        generation=2,
    )

    assert parsed.generation == 2
    assert parsed.findings == ["retained finding"]


def test_manifest_builder_groups_applicable_controls_by_module_and_surface() -> None:
    profile = load_profile(PROJECT_ROOT / "examples/app-profile.yaml")
    controls = load_controls(PROJECT_ROOT / "examples/mvp-controls.yaml")

    manifest = ReviewManifestBuilder().build(profile, controls, run_id="run-day3")

    assert manifest.contract == "review_manifest.v1"
    assert manifest.default_max_concurrency == 3
    assert len(manifest.work_items) >= 3
    assert not manifest.excluded_controls
    assert all(item.allowed_roots for item in manifest.work_items)
    assert len({item.work_item_id for item in manifest.work_items}) == len(manifest.work_items)


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


def test_openai_compatible_provider_uses_tools_then_strict_terminal_finalization(
    tmp_path: Path,
) -> None:
    item = _work_item("wi.strict-finalization")
    calls: list[ModelRequest] = []

    def response_factory(request: ModelRequest) -> ModelResponse:
        calls.append(request)
        if request.request_kind == "review":
            if len([call for call in calls if call.request_kind == "review"]) == 1:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            call_id="read-app",
                            name="read_file",
                            arguments={"path": "backend/app.py", "start_line": 1, "line_count": 4},
                        )
                    ]
                )
            # The candidate need not itself be schema-valid; finalization owns the
            # one strict result and must not re-enter the tool loop.
            return ModelResponse(content="Candidate: the endpoint needs review.")
        assert request.request_kind == "review_finalization"
        assert request.tools == []
        assert request.response_schema is not None
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    provider = StaticModelProvider(response_factory)
    provider.supports_strict_finalization = True
    summary = LangGraphReviewRuntime(provider=provider, max_concurrency=1).run(
        manifest_run_id="run-strict-finalization",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 1
    assert [request.request_kind for request in calls] == [
        "review",
        "review",
        "review_finalization",
    ]
    assert calls[-1].tools == []


def test_model_failure_retries_without_overwriting_attempt_history(tmp_path: Path) -> None:
    item = _work_item("wi.retry")
    calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("temporary provider timeout")
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=1,
        max_attempts=2,
    ).run(
        manifest_run_id="run-retry",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert calls == 2
    assert summary.completed == 1
    assert summary.failed == 0
    assert [attempt.status for attempt in summary.attempts] == ["failed", "completed"]
    assert (tmp_path / "work-items" / item.work_item_id / "attempts" / "attempt-001").is_dir()
    assert (tmp_path / "work-items" / item.work_item_id / "attempts" / "attempt-002").is_dir()


def test_retryable_tool_failure_starts_a_new_attempt(tmp_path: Path) -> None:
    item = _work_item("wi.tool-retry")
    calls = 0

    class FlakyCodeMap:
        def query(self, request: object) -> CodeMapQueryResult:
            if not hasattr(self, "failed_once"):
                self.failed_once = True
                raise OSError("temporary graph service failure")
            return CodeMapQueryResult(query="loan", surface="backend_code", status="available")

        def path(self, request: object) -> object:
            raise AssertionError("path should not be called")

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls in {1, 2}:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id=f"graph-{calls}",
                        name="code_map_query",
                        arguments={"query": "loan"},
                    )
                ]
            )
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=1,
        max_attempts=2,
        code_map_providers={"backend_code": FlakyCodeMap()},
    ).run(
        manifest_run_id="run-tool-retry",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert calls == 3
    assert summary.completed == 1
    assert [attempt.status for attempt in summary.attempts] == ["failed", "completed"]


def test_non_retryable_path_escape_keeps_one_failed_attempt(tmp_path: Path) -> None:
    item = _work_item("wi.path-escape")

    def response_factory(request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    call_id="escape",
                    name="read_file",
                    arguments={"path": "../secret.txt"},
                )
            ]
        )

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory), max_concurrency=1, max_attempts=3
    ).run(
        manifest_run_id="run-path-escape",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.failed == 1
    assert len(summary.attempts) == 1
    assert summary.attempts[0].error_code == "path_escape"
    assert (tmp_path / "work-items" / item.work_item_id / "attempts" / "attempt-001").is_dir()


def test_completed_attempt_is_reused_after_resume(tmp_path: Path) -> None:
    item = _work_item("wi.resume")
    calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    first = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory), max_concurrency=1
    ).run(
        manifest_run_id="run-resume",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )
    second = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory), max_concurrency=1
    ).run(
        manifest_run_id="run-resume",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert first.completed == second.completed == 1
    assert calls == 1
    events = [
        json.loads(line) for line in (tmp_path / "worker-events.jsonl").read_text().splitlines()
    ]
    assert any(event["event_type"] == "worker_resume_skipped" for event in events)


def test_fingerprint_mismatch_does_not_reuse_completed_attempt(tmp_path: Path) -> None:
    item = _work_item("wi.fingerprint")
    calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    provider = StaticModelProvider(response_factory)
    LangGraphReviewRuntime(provider=provider, max_concurrency=1).run(
        manifest_run_id="run-fingerprint",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )
    changed_item = item.model_copy(update={"control_ids": ["control.changed"]})
    second = LangGraphReviewRuntime(provider=provider, max_concurrency=1).run(
        manifest_run_id="run-fingerprint",
        work_items=[changed_item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert calls == 2
    assert second.completed == 1
    assert second.executions[0].attempt_number == 2


def test_stale_running_attempt_is_interrupted_before_resume(tmp_path: Path) -> None:
    item = _work_item("wi.stale")
    output_root = tmp_path / "work-items"
    stale = WorkerAttempt(
        work_item_id=item.work_item_id,
        attempt_id="run-stale.wi.stale.attempt-001-old",
        attempt_number=1,
        status="running",
        started_at="2026-01-01T00:00:00+00:00",
        retryable=True,
        context_fingerprint="stale-fingerprint",
    )
    attempt_dir = output_root / item.work_item_id / "attempts" / "attempt-001"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "attempt.json").write_text(stale.model_dump_json(indent=2), encoding="utf-8")

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(
            lambda request: ModelResponse(
                content=_review_json(request, request.work_item, request.agent_id)
            )
        ),
        max_concurrency=1,
        max_attempts=2,
    ).run(
        manifest_run_id="run-stale",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=output_root,
    )

    assert summary.completed == 1
    assert [attempt.status for attempt in summary.attempts] == ["interrupted", "completed"]


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


def test_reviewer_tools_include_graphify_and_collector_facts() -> None:
    class FakeCodeMapProvider:
        def query(self, request: object) -> CodeMapQueryResult:
            return CodeMapQueryResult(
                query="account deletion",
                surface="backend_code",
                status="available",
                candidates=[
                    CodeMapCandidate(symbol="AccountService.delete", path="backend/app.py"),
                    CodeMapCandidate(symbol="SecretStore.read", path="secrets.txt"),
                ],
                relations=[
                    CodeMapRelation(
                        source="AccountService.delete",
                        relation="calls",
                        target="SecretStore.read",
                    )
                ],
            )

        def path(self, request: object) -> object:
            raise AssertionError("path is not needed in this test")

    collector = CollectorResult(
        collector_id="android_manifest",
        source_surface="backend_code",
        parser_status="ok",
        coverage_status="complete",
        facts=[
            Fact(
                fact_id="fact.backend.permission.contacts",
                source_surface="backend_code",
                fact_type="android_manifest_permission",
                observed_value="android.permission.READ_CONTACTS",
                source_refs=[SourceRef(path="app/src/main/AndroidManifest.xml")],
                parser_status="ok",
                coverage_status="complete",
                evidence_strength="static_proof",
            )
        ],
    )
    work_item = _work_item("wi.graph-tools").model_copy(
        update={"collector_fact_refs": ["fact.backend.permission.contacts"]}
    )
    executor = ScopedToolExecutor(
        RepositorySandbox(FIXTURES),
        work_item,
        code_map_provider=FakeCodeMapProvider(),
        collector_results={"android_manifest": collector},
    )

    code_map = executor.execute(
        ToolCall(
            call_id="map-1",
            name="code_map_query",
            arguments={"query": "account deletion"},
        )
    )
    facts = executor.execute(
        ToolCall(
            call_id="facts-1",
            name="get_collector_facts",
            arguments={"collector_id": "android_manifest"},
        )
    )

    assert code_map.ok is True
    assert [item["symbol"] for item in code_map.output["candidates"]] == ["AccountService.delete"]
    assert code_map.output["relations"] == []
    assert facts.ok is True
    assert facts.output["facts"][0]["fact_id"] == "fact.backend.permission.contacts"


def test_collector_fact_tool_denies_unassigned_fact_capabilities() -> None:
    allowed = Fact(
        fact_id="fact.allowed",
        source_surface="backend_code",
        fact_type="backend_presence",
        observed_value=True,
        source_refs=[SourceRef(path="backend/app.py")],
        parser_status="ok",
        coverage_status="complete",
        evidence_strength="server_code",
    )
    denied = allowed.model_copy(update={"fact_id": "fact.denied"})
    collector = CollectorResult(
        collector_id="dependencies",
        source_surface="backend_code",
        parser_status="ok",
        coverage_status="complete",
        facts=[allowed, denied],
    )
    work_item = _work_item("wi.fact-capability").model_copy(
        update={"collector_fact_refs": [allowed.fact_id]}
    )
    executor = ScopedToolExecutor(
        RepositorySandbox(FIXTURES),
        work_item,
        collector_results={"repo/dependencies": collector},
    )

    listed = executor.execute(
        ToolCall(call_id="facts-listed", name="get_collector_facts", arguments={})
    )
    rejected = executor.execute(
        ToolCall(
            call_id="facts-rejected",
            name="get_collector_facts",
            arguments={"fact_ids": [denied.fact_id]},
        )
    )

    assert [item["fact_id"] for item in listed.output["facts"]] == [allowed.fact_id]
    assert rejected.ok is False
    assert "outside the Work Item capability" in (rejected.error or "")


def test_tool_schema_exposes_all_read_only_reviewer_tools() -> None:
    names = {schema["function"]["name"] for schema in tool_schemas()}
    assert names == {
        "code_map_query",
        "code_map_path",
        "get_collector_facts",
        "list_files",
        "search_code",
        "read_file",
    }


def test_work_items_have_independent_contexts_and_tool_histories(tmp_path: Path) -> None:
    items = [_work_item("wi.context-a"), _work_item("wi.context-b")]
    request_log: dict[str, list[ModelRequest]] = {item.work_item_id: [] for item in items}
    call_counts: dict[str, int] = {item.work_item_id: 0 for item in items}

    def response_factory(request: ModelRequest) -> ModelResponse:
        work_item_id = request.work_item.work_item_id
        request_log[work_item_id].append(request)
        call_counts[work_item_id] += 1
        if call_counts[work_item_id] == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id=f"read-{work_item_id}",
                        name="read_file",
                        arguments={"path": "backend/app.py", "line_count": 2},
                    )
                ]
            )
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=1,
    ).run(
        manifest_run_id="run-context-isolation",
        work_items=items,
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 2
    first_a, second_a = request_log["wi.context-a"]
    first_b, second_b = request_log["wi.context-b"]
    assert len(first_a.messages) == 2
    assert len(first_b.messages) == 2
    assert "wi.context-a" in first_a.messages[-1]["content"]
    assert "wi.context-b" in first_b.messages[-1]["content"]
    assert "wi.context-b" not in first_a.messages[-1]["content"]
    assert "wi.context-a" not in first_b.messages[-1]["content"]
    assert any(message.get("tool_call_id") == "read-wi.context-a" for message in second_a.messages)
    assert any(message.get("tool_call_id") == "read-wi.context-b" for message in second_b.messages)
    assert all("read-wi.context-b" not in str(message) for message in second_a.messages)
    assert all("read-wi.context-a" not in str(message) for message in second_b.messages)


def test_runtime_enforces_tool_budgets_across_rounds(tmp_path: Path) -> None:
    work_item = _work_item("wi.cumulative-budget").model_copy(
        update={"max_files_read": 1, "max_tool_rounds": 3}
    )
    calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="read-first",
                        name="read_file",
                        arguments={"path": "backend/app.py", "line_count": 1},
                    )
                ]
            )
        if calls == 2:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="read-second",
                        name="read_file",
                        arguments={"path": "backend/build.gradle.kts", "line_count": 1},
                    )
                ]
            )
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    provider = StaticModelProvider(response_factory)
    LangGraphReviewRuntime(provider=provider, max_concurrency=1).run(
        manifest_run_id="run-cumulative-budget",
        work_items=[work_item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    second_tool_result = next(
        message
        for message in provider.requests[-1].messages
        if message.get("tool_call_id") == "read-second"
    )
    assert '"ok": false' in second_tool_result["content"]
    assert "max_files_read exceeded" in second_tool_result["content"]


def test_runtime_rejects_extra_and_duplicate_reviewer_rows(tmp_path: Path) -> None:
    extra = _work_item("wi.extra-row")
    duplicate = _work_item("wi.duplicate-row")

    def response_factory(request: ModelRequest) -> ModelResponse:
        rows = [
            ControlSurfaceResult(
                control_id=request.work_item.control_ids[0],
                surface=request.work_item.surface,
                evidence_status="missing",
                recommended_control_status="indeterminate",
            )
        ]
        if request.work_item.work_item_id == extra.work_item_id:
            rows.append(
                ControlSurfaceResult(
                    control_id="control.out-of-scope",
                    surface=request.work_item.surface,
                    evidence_status="missing",
                    recommended_control_status="indeterminate",
                )
            )
        else:
            rows.append(rows[0])
        result = ReviewResult(
            contract="review_result.v1",
            work_item_id=request.work_item.work_item_id,
            attempt_id=request.attempt_id,
            execution_status="completed",
            rows=rows,
            agent_id=request.agent_id,
        )
        return ModelResponse(content=result.model_dump_json())

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=2,
    ).run(
        manifest_run_id="run-invalid-rows",
        work_items=[extra, duplicate],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 0
    assert summary.failed == 2
    errors = {execution.work_item_id: execution.error for execution in summary.executions}
    assert "exactly match assigned controls" in (errors[extra.work_item_id] or "")
    assert "duplicate control-surface rows" in (errors[duplicate.work_item_id] or "")


def test_work_item_failure_does_not_pollute_another_context(tmp_path: Path) -> None:
    failed = _work_item("wi.failed")
    healthy = _work_item("wi.healthy")
    requests: list[ModelRequest] = []

    def response_factory(request: ModelRequest) -> ModelResponse:
        requests.append(request)
        if request.work_item.work_item_id == failed.work_item_id:
            raise RuntimeError("provider failure for failed work item")
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=2,
    ).run(
        manifest_run_id="run-failure-isolation",
        work_items=[failed, healthy],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 1
    assert summary.failed == 1
    healthy_request = next(
        request for request in requests if request.work_item.work_item_id == healthy.work_item_id
    )
    assert "provider failure for failed work item" not in str(healthy_request.messages)


def test_seven_work_items_are_bounded_and_waiting_items_progress(tmp_path: Path) -> None:
    items = [_work_item(f"wi.queue-{index}") for index in range(7)]
    active = 0
    completed = 0
    maximum_active = 0
    start_records: list[tuple[str, int]] = []
    lock = threading.Lock()

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal active, completed, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            start_records.append((request.work_item.work_item_id, completed))
        time.sleep(0.03)
        with lock:
            active -= 1
            completed += 1
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=3,
    ).run(
        manifest_run_id="run-queue",
        work_items=items,
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 7
    assert summary.failed == 0
    assert maximum_active <= 3
    assert len(start_records) == 7
    assert any(completed_before_start >= 1 for _, completed_before_start in start_records[3:])


def test_context_budget_exhaustion_completes_with_bounded_indeterminate_result(
    tmp_path: Path,
) -> None:
    item = _work_item("wi.context-budget")

    def response_factory(request: ModelRequest) -> ModelResponse:
        raise AssertionError("model should not be called after the hard context gate")

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=1,
        context_config=ReviewerRuntimeConfig(context_window_tokens=100),
    ).run(
        manifest_run_id="run-context-budget",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 1
    assert summary.failed == 0
    execution = summary.executions[0]
    assert execution.error is None
    assert execution.result is not None
    assert execution.result.rows[0].recommended_control_status == "indeterminate"
    assert execution.result.rows[0].gap_reasons == ["context_budget_exhausted"]


def test_tool_budgets_remain_cumulative_across_model_rounds(tmp_path: Path) -> None:
    item = _work_item("wi.cumulative-budget").model_copy(
        update={"max_files_read": 1, "max_tool_rounds": 3}
    )
    calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="read-first",
                        name="read_file",
                        arguments={"path": "backend/app.py", "line_count": 1},
                    )
                ]
            )
        if calls == 2:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="read-second",
                        name="read_file",
                        arguments={"path": "backend/build.gradle.kts", "line_count": 1},
                    )
                ]
            )
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    provider = StaticModelProvider(response_factory)
    summary = LangGraphReviewRuntime(provider=provider, max_concurrency=1).run(
        manifest_run_id="run-cumulative-budget",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    assert summary.completed == 1
    assert summary.failed == 0
    assert len(provider.requests) == 3
    assert "max_files_read exceeded" in provider.requests[2].messages[-1]["content"]


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
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

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


def test_runtime_discards_provider_supplied_anchors_without_tool_provenance(
    tmp_path: Path,
) -> None:
    item = _work_item("wi.fabricated-anchor")

    def response_factory(request: ModelRequest) -> ModelResponse:
        fabricated = EvidenceAnchor(
            anchor_id="anchor.fabricated",
            control_ids=[item.control_ids[0]],
            source_surface="backend_code",
            source_tool="read_file",
            path="backend/app.py",
            exact_snippet="return {'status': 'ok'}",
            normalized_snippet_hash="not-a-real-hash",
            evidence_strength="server_code",
            summary="Provider-supplied anchor without a tool call.",
        )
        result = ReviewResult(
            contract="review_result.v1",
            work_item_id=item.work_item_id,
            attempt_id=request.attempt_id,
            execution_status="completed",
            rows=[
                ControlSurfaceResult(
                    control_id=item.control_ids[0],
                    surface="backend_code",
                    evidence_status="complete",
                    recommended_control_status="pass",
                    observed_evidence_strength="server_code",
                    anchor_ids=[fabricated.anchor_id],
                )
            ],
            anchors=[fabricated],
            agent_id=request.agent_id,
        )
        return ModelResponse(content=result.model_dump_json())

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory), max_concurrency=1
    ).run(
        manifest_run_id="run-fabricated-anchor",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
    )

    result = summary.executions[0].result
    assert result is not None
    assert result.anchors == []
    assert result.rows[0].anchor_ids == ["anchor.fabricated"]


def test_langgraph_runtime_persists_parent_checkpoint(tmp_path: Path) -> None:
    item = _work_item("wi.checkpoint")

    def response_factory(request: ModelRequest) -> ModelResponse:
        return ModelResponse(content=_review_json(request, request.work_item, request.agent_id))

    runtime = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=1,
    )
    summary = runtime.run(
        manifest_run_id="run-checkpoint",
        work_items=[item],
        sandboxes={"backend_code": RepositorySandbox(FIXTURES)},
        output_root=tmp_path / "work-items",
        event_log_path=tmp_path / "events.jsonl",
        thread_id="thread-checkpoint",
    )

    assert summary.completed == 1
    checkpoint = runtime.checkpointer.get_tuple(
        {"configurable": {"thread_id": "thread-checkpoint"}}
    )
    assert checkpoint is not None
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert '"runtime": "langgraph"' in events


def test_anchor_generation_preserves_search_location_and_fact_provenance() -> None:
    sandbox = RepositorySandbox(FIXTURES)
    work_item = _work_item("wi.anchor-contract")
    search_anchors = _anchors_from_tool_results(
        work_item,
        [
            ToolCall(
                call_id="search-1",
                name="search_code",
                arguments={"query": "loan/disburse"},
            )
        ],
        [
            ScopedToolResult(
                call_id="search-1",
                name="search_code",
                ok=True,
                output=[
                    {
                        "path": "backend/app.py",
                        "line_number": 3,
                        "line_text": "@app.post('/loan/disburse')",
                    }
                ],
            )
        ],
        sandbox,
    )
    assert search_anchors[0].start_line == 3
    assert search_anchors[0].end_line == 3
    assert search_anchors[0].file_revision == file_content_revision(
        (FIXTURES / "backend" / "app.py").read_bytes()
    )

    read_anchors = _anchors_from_tool_results(
        work_item,
        [
            ToolCall(
                call_id="read-1",
                name="read_file",
                arguments={"path": "backend/app.py", "start_line": 3, "line_count": 20},
            )
        ],
        [
            ScopedToolResult(
                call_id="read-1",
                name="read_file",
                ok=True,
                output="@app.post('/loan/disburse')\ndef disburse():\n    return {'status': 'ok'}",
            )
        ],
        sandbox,
    )
    assert read_anchors[0].start_line == 3
    assert read_anchors[0].end_line == 5

    fact_anchors = _anchors_from_tool_results(
        work_item,
        [ToolCall(call_id="facts-1", name="get_collector_facts", arguments={})],
        [
            ScopedToolResult(
                call_id="facts-1",
                name="get_collector_facts",
                ok=True,
                output={
                    "facts": [
                        {
                            "fact_id": "fact.one",
                            "evidence_strength": "declared",
                            "source_refs": [{"path": "backend/app.py", "start_line": 1}],
                        },
                        {
                            "fact_id": "fact.two",
                            "evidence_strength": "server_code",
                            "source_refs": [{"path": "backend/app.py", "start_line": 2}],
                        },
                    ]
                },
            )
        ],
        sandbox,
    )
    assert [anchor.fact_ids for anchor in fact_anchors] == [["fact.one"], ["fact.two"]]
    assert [anchor.evidence_strength for anchor in fact_anchors] == [
        "declared",
        "server_code",
    ]
