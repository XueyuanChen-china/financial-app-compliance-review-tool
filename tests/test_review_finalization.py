from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import (
    Control,
    ControlSet,
    ControlSurfaceResult,
    CoverageSet,
    CoverageUnit,
    EvidenceAnchor,
    Fact,
    ReviewResult,
    SourceRef,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.evidence import file_content_revision
from compliance_review.review.finalization import (
    ComplianceResolver,
    CoverageGate,
    ResultValidator,
    SuspiciousRouter,
    TargetedVerifier,
)
from compliance_review.review.models import (
    ModelRequest,
    ModelResponse,
    ResultValidationResult,
    ReviewRunSummary,
    VerifierDecision,
    VerifierResult,
    WorkerExecution,
)
from compliance_review.review.provider import StaticModelProvider


def _control() -> Control:
    return Control(
        control_id="privacy.backend_required",
        module_id="privacy",
        title="Backend evidence is required",
        severity="critical",
        applicability_expression="self_lending == true",
        required_surfaces=["frontend_h5", "backend_code"],
        minimum_evidence_strength={
            "frontend_h5": "static_proof",
            "backend_code": "server_code",
        },
        missing_evidence_policy="block",
        source_refs=[SourceRef(url="https://example.test/policy")],
        reuse_invalidation_keys=["control_version"],
    )


def _coverage() -> CoverageSet:
    return CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        missing_surfaces=["backend_code"],
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.frontend_h5",
                control_id="privacy.backend_required",
                module_id="privacy",
                surface="frontend_h5",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="static_proof",
                reason="fixture",
                work_item_id="wi.privacy.frontend_h5",
            ),
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.backend_code",
                control_id="privacy.backend_required",
                module_id="privacy",
                surface="backend_code",
                applicability_status="applicable",
                coverage_status="missing_surface",
                required_evidence_strength="server_code",
                reason="backend repository unavailable",
                work_item_id="wi.privacy.backend_code",
            ),
        ],
    )


def _frontend_summary(repo: Path) -> ReviewRunSummary:
    snippet = "const consent = true;"
    normalized_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
    anchor = EvidenceAnchor(
        anchor_id="anchor.frontend.consent",
        control_ids=["privacy.backend_required"],
        source_surface="frontend_h5",
        source_tool="read_file",
        path="src/consent.js",
        start_line=1,
        end_line=1,
        exact_snippet=snippet,
        normalized_snippet_hash=normalized_hash,
        file_revision=file_content_revision((repo / "src" / "consent.js").read_bytes()),
        evidence_strength="static_proof",
        summary="Consent setting",
    )
    result = ReviewResult(
        contract="review_result.v1",
        work_item_id="wi.privacy.frontend_h5",
        attempt_id="attempt-1",
        execution_status="completed",
        rows=[
            {
                "control_id": "privacy.backend_required",
                "surface": "frontend_h5",
                "evidence_status": "complete",
                "recommended_control_status": "pass",
                "observed_evidence_strength": "static_proof",
                "anchor_ids": [anchor.anchor_id],
                "confidence": "high",
            }
        ],
        anchors=[anchor],
        agent_id="reviewer-001",
    )
    return ReviewRunSummary(
        run_id="run-day4",
        executions=[
            WorkerExecution(
                work_item_id=result.work_item_id,
                attempt_id=result.attempt_id,
                agent_id=result.agent_id,
                execution_status="completed",
                result_path=(repo / "review-result.json").as_posix(),
                result=result,
                context_fingerprint="fingerprint",
            )
        ],
        max_concurrency=3,
        completed=1,
        failed=0,
        event_log_path=(repo / "events.jsonl").as_posix(),
    )


def test_backend_gap_prevents_reviewer_pass_from_becoming_final_pass(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[_control()])
    coverage = _coverage()
    summary = _frontend_summary(frontend)
    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )

    suspicious = SuspiciousRouter().route(validation)
    resolved = ComplianceResolver().resolve(controls, coverage, validation, verifier_result=None)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved)

    assert "privacy.backend_required:backend_code" not in suspicious.row_ids
    assert resolved[0].status == "indeterminate"
    assert gate.ci_status == "block"
    assert gate.complete is False
    rows = {row.coverage_unit_id: row for row in gate.rows}
    assert rows["cu.privacy.backend_required.backend_code"].execution_status == "pending"
    assert rows["cu.privacy.backend_required.backend_code"].evidence_status == "missing"


def test_high_severity_minimum_threshold_pass_is_valid_but_flagged(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    control = _control().model_copy(
        update={
            "severity": "high",
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    validation = ResultValidator().validate(
        _frontend_summary(frontend),
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved, mode="full")

    assert validation.valid is True
    assert resolved[0].status == "pass"
    assert {"high_severity_pass", "minimum_threshold_pass"}.issubset(
        set(validation.flags["privacy.backend_required:frontend_h5"])
    )
    assert gate.ci_status == "pass"


def test_low_confidence_and_unsupported_pass_are_errors(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    summary = _frontend_summary(frontend)
    result = summary.executions[0].result
    assert result is not None
    row = result.rows[0].model_copy(
        update={"confidence": "low", "unsupported_inferences": ["unverified path"]}
    )
    summary = summary.model_copy(
        update={
            "executions": [
                summary.executions[0].model_copy(
                    update={"result": result.model_copy(update={"rows": [row]})}
                )
            ]
        }
    )
    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation)

    assert validation.valid is False
    assert any("low_confidence" in error for error in validation.errors)
    assert any("unsupported_inference" in error for error in validation.errors)
    assert resolved[0].status == "indeterminate"


def test_full_external_manual_requirement_is_reported_without_ci_warning() -> None:
    control = _control().model_copy(
        update={
            "required_surfaces": ["play_console"],
            "minimum_evidence_strength": {"play_console": "declared"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.play_console",
                control_id=control.control_id,
                module_id="privacy",
                surface="play_console",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="declared",
                reason="manual store evidence",
            )
        ],
    )
    validation = ResultValidationResult(valid=True)
    resolved = ComplianceResolver().resolve(controls, coverage, validation)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved, mode="full")

    assert resolved[0].status == "indeterminate"
    assert gate.complete is True
    assert gate.ci_status == "pass"
    assert gate.warning_reasons == []
    assert gate.manual_review_existing_ids == ["cu.privacy.backend_required.play_console"]


def test_mixed_automated_and_external_gap_does_not_block_complete_automation(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5", "play_console"],
            "minimum_evidence_strength": {
                "frontend_h5": "static_proof",
                "play_console": "declared",
            },
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    frontend_unit = _coverage().units[0]
    play_unit = CoverageUnit(
        coverage_unit_id="cu.privacy.backend_required.play_console",
        control_id=control.control_id,
        module_id="privacy",
        surface="play_console",
        applicability_status="applicable",
        coverage_status="planned",
        required_evidence_strength="declared",
        reason="manual store evidence",
    )
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        units=[frontend_unit, play_unit],
    )
    validation = ResultValidator().validate(
        _frontend_summary(frontend),
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved, mode="full")

    assert resolved[0].status == "indeterminate"
    assert gate.complete is True
    assert gate.ci_status == "pass"
    assert gate.manual_review_existing_ids == [play_unit.coverage_unit_id]


def test_diff_manual_delta_and_automated_regression_policy() -> None:
    control = _control().model_copy(
        update={
            "required_surfaces": ["play_console"],
            "minimum_evidence_strength": {"play_console": "declared"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.play_console",
                control_id=control.control_id,
                module_id="privacy",
                surface="play_console",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="declared",
                reason="manual store evidence",
            )
        ],
    )
    validation = ResultValidationResult(valid=True)
    resolved = ComplianceResolver().resolve(controls, coverage, validation)
    unit_id = coverage.units[0].coverage_unit_id
    new_gate = CoverageGate().evaluate(
        controls,
        coverage,
        validation,
        resolved,
        mode="diff",
        previous_manual_ids=[],
    )
    existing_gate = CoverageGate().evaluate(
        controls,
        coverage,
        validation,
        resolved,
        mode="diff",
        previous_manual_ids=[unit_id],
    )
    blocked_gate = CoverageGate().evaluate(
        controls,
        coverage,
        validation,
        resolved,
        mode="diff",
        automated_evidence_regression_ids=[unit_id],
    )

    assert new_gate.ci_status == "warn"
    assert new_gate.manual_review_new_ids == [unit_id]
    assert existing_gate.ci_status == "pass"
    assert existing_gate.manual_review_existing_ids == [unit_id]
    assert blocked_gate.ci_status == "block"
    assert blocked_gate.automated_evidence_regression_ids == [unit_id]


def test_failed_worker_becomes_indeterminate_and_blocked_coverage() -> None:
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.frontend_h5",
                control_id=control.control_id,
                module_id="privacy",
                surface="frontend_h5",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="static_proof",
                reason="worker failure fixture",
                work_item_id="wi.failed.frontend",
            )
        ],
    )
    failed_result = ReviewResult(
        contract="review_result.v1",
        work_item_id="wi.failed.frontend",
        attempt_id="attempt.failed",
        execution_status="failed",
        rows=[
            {
                "control_id": control.control_id,
                "surface": "frontend_h5",
                "evidence_status": "missing",
                "recommended_control_status": "indeterminate",
            }
        ],
        agent_id="reviewer-001",
    )
    summary = ReviewRunSummary(
        run_id="run-failed-worker",
        executions=[
            WorkerExecution(
                work_item_id="wi.failed.frontend",
                attempt_id="attempt.failed",
                agent_id="reviewer-001",
                execution_status="failed",
                result_path="failed.json",
                result=failed_result,
                error_code="worker_error",
                context_fingerprint="fingerprint",
            )
        ],
        max_concurrency=1,
        completed=0,
        failed=1,
        event_log_path="events.jsonl",
    )

    validation = ResultValidator().validate(summary, coverage, controls, {})
    resolved = ComplianceResolver().resolve(controls, coverage, validation, None)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved)

    assert resolved[0].status == "indeterminate"
    assert resolved[0].status != "fail"
    assert gate.rows[0].execution_status == "failed"
    assert gate.rows[0].evidence_status == "missing"
    assert gate.ci_status == "block"


def test_validator_rejects_cross_work_item_and_failed_execution_claims(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[_control()])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    summary = _frontend_summary(frontend)
    original = summary.executions[0]
    injected = original.model_copy(
        update={
            "work_item_id": "wi.other.frontend_h5",
            "execution_status": "failed",
            "error": "forged failure with completed result",
        }
    )
    summary = summary.model_copy(update={"executions": [injected], "completed": 0, "failed": 1})

    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
        work_items=[
            WorkItem(
                work_item_id="wi.privacy.frontend_h5",
                module_id="privacy",
                surface="frontend_h5",
                control_ids=["privacy.backend_required"],
            )
        ],
    )

    row = validation.rows[0]
    assert row.valid is False
    assert "work_item_claim_mismatch" in {issue.code for issue in row.issues}


def test_invalid_anchor_is_suspicious_and_cannot_support_pass(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = false;\n", encoding="utf-8")
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[_control()])
    validation = ResultValidator().validate(
        _frontend_summary(frontend),
        _coverage(),
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )

    frontend_row = next(row for row in validation.rows if row.surface == "frontend_h5")
    assert frontend_row.valid is False
    assert "anchor_snippet_not_found" in {issue.code for issue in frontend_row.issues}


def test_validator_rejects_unknown_or_unassigned_collector_fact(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[_control()])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    summary = _frontend_summary(frontend)
    result = summary.executions[0].result
    assert result is not None
    anchor = result.anchors[0].model_copy(update={"fact_ids": ["fact.unassigned"]})
    row = result.rows[0].model_copy(update={"fact_ids": ["fact.unassigned"]})
    result = result.model_copy(update={"anchors": [anchor], "rows": [row]})
    summary = summary.model_copy(
        update={"executions": [summary.executions[0].model_copy(update={"result": result})]}
    )
    fact = Fact(
        fact_id="fact.unassigned",
        source_surface="frontend_h5",
        fact_type="frontend_framework",
        observed_value="react",
        source_refs=[SourceRef(path="src/consent.js")],
        parser_status="ok",
        coverage_status="complete",
        evidence_strength="static_proof",
    )
    collector = CollectorResult(
        collector_id="dependencies",
        source_surface="frontend_h5",
        parser_status="ok",
        coverage_status="complete",
        facts=[fact],
    )

    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
        work_items=[
            WorkItem(
                work_item_id="wi.privacy.frontend_h5",
                module_id="privacy",
                surface="frontend_h5",
                control_ids=["privacy.backend_required"],
                collector_fact_refs=[],
            )
        ],
        collector_results={"frontend/dependencies": collector},
    )

    frontend_row = validation.rows[0]
    assert frontend_row.valid is False
    assert "fact_out_of_scope" in {issue.code for issue in frontend_row.issues}


def test_partial_api_document_fact_can_support_declared_endpoint_evidence(
    tmp_path: Path,
) -> None:
    api_docs = tmp_path / "api-docs"
    api_docs.mkdir()
    document = api_docs / "openapi.json"
    document.write_text('{"paths":{"/v1/delete":{"delete":{}}}}', encoding="utf-8")
    control = _control().model_copy(
        update={
            "required_surfaces": ["backend_api_doc"],
            "minimum_evidence_strength": {"backend_api_doc": "server_doc"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.backend_api_doc",
                control_id=control.control_id,
                module_id="privacy",
                surface="backend_api_doc",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="server_doc",
                reason="fixture",
                work_item_id="wi.privacy.backend_api_doc",
            )
        ],
    )
    snippet = document.read_text(encoding="utf-8")
    anchor = EvidenceAnchor(
        anchor_id="anchor.api.delete",
        control_ids=[control.control_id],
        source_surface="backend_api_doc",
        source_tool="get_collector_facts",
        path="openapi.json",
        start_line=1,
        end_line=1,
        exact_snippet=snippet,
        normalized_snippet_hash=hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
        file_revision=file_content_revision(document.read_bytes()),
        evidence_strength="server_doc",
        fact_ids=["fact.api.delete"],
        summary="Declared delete endpoint",
    )
    result = ReviewResult(
        contract="review_result.v1",
        work_item_id="wi.privacy.backend_api_doc",
        attempt_id="attempt-api",
        execution_status="completed",
        rows=[
            {
                "control_id": control.control_id,
                "surface": "backend_api_doc",
                "evidence_status": "complete",
                "recommended_control_status": "pass",
                "observed_evidence_strength": "server_doc",
                "anchor_ids": [anchor.anchor_id],
                "fact_ids": ["fact.api.delete"],
                "confidence": "high",
            }
        ],
        anchors=[anchor],
        agent_id="reviewer-001",
    )
    summary = ReviewRunSummary(
        run_id="run-api",
        executions=[
            WorkerExecution(
                work_item_id=result.work_item_id,
                attempt_id=result.attempt_id,
                agent_id=result.agent_id,
                execution_status="completed",
                result_path=(api_docs / "result.json").as_posix(),
                result=result,
                context_fingerprint="api",
            )
        ],
        max_concurrency=1,
        completed=1,
        failed=0,
        event_log_path=(api_docs / "events.jsonl").as_posix(),
    )
    fact = Fact(
        fact_id="fact.api.delete",
        source_surface="backend_api_doc",
        fact_type="declared_api_endpoint",
        observed_value={"method": "DELETE", "route": "/v1/delete"},
        source_refs=[SourceRef(path="openapi.json")],
        parser_status="ok",
        coverage_status="partial",
        evidence_strength="server_doc",
    )
    collector = CollectorResult(
        collector_id="api_document_inventory",
        source_surface="backend_api_doc",
        parser_status="ok",
        coverage_status="complete",
        facts=[fact],
    )

    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"backend_api_doc": RepositorySandbox(api_docs)},
        work_items=[
            WorkItem(
                work_item_id="wi.privacy.backend_api_doc",
                module_id="privacy",
                surface="backend_api_doc",
                control_ids=[control.control_id],
                collector_fact_refs=[fact.fact_id],
            )
        ],
        collector_results={"api": collector},
    )

    assert validation.rows[0].valid is True
    assert "fact_not_authoritative" not in {issue.code for issue in validation.rows[0].issues}


def test_dirty_file_content_revision_relocates_unique_anchor(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    target = frontend / "src" / "consent.js"
    target.write_text("const consent = true;\nconst mode = 'old';\n", encoding="utf-8")
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[_control()])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    summary = _frontend_summary(frontend)
    target.write_text("const consent = true;\nconst mode = 'new';\n", encoding="utf-8")

    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )

    row = validation.rows[0]
    assert row.valid is True
    assert "anchor_relocated" in {issue.code for issue in row.issues}


def test_targeted_verifier_runs_one_structured_call_for_suspicious_rows(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[_control()])
    validation = ResultValidator().validate(
        _frontend_summary(frontend),
        _coverage(),
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    suspicious = SuspiciousRouter().route(validation)

    def response_factory(request: ModelRequest) -> ModelResponse:
        assert request.request_kind == "verification"
        assert request.tools == []
        payload = json.loads(request.messages[-1]["content"])
        anchors_by_row = {row["row_id"]: row["row"]["anchor_ids"] for row in payload["rows"]}
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "verifier_result.v1",
                    "status": "completed",
                    "decisions": [
                        {
                            "row_id": row_id,
                            "decision": "confirm",
                            "reason": "The deterministic concern is confirmed.",
                            "anchor_ids": anchors_by_row[row_id],
                        }
                        for row_id in suspicious.row_ids
                    ],
                }
            )
        )

    provider = StaticModelProvider(response_factory)
    result = TargetedVerifier(provider).verify(suspicious, validation, controls)

    assert result.status == "completed"
    assert len(provider.requests) == 1
    assert {item.row_id for item in result.decisions} == set(suspicious.row_ids)


def test_contradictory_verifier_confirm_is_partial_and_cannot_authorize_pass(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    validation = ResultValidator().validate(
        _frontend_summary(frontend),
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    suspicious = SuspiciousRouter().route(validation)

    def response_factory(request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "verifier_result.v1",
                    "status": "completed",
                    "decisions": [
                        {
                            "row_id": suspicious.row_ids[0],
                            "decision": "confirm",
                            "reason": "Contradictory confirmation.",
                            "recommended_status": "fail",
                            "anchor_ids": [],
                        }
                    ],
                }
            )
        )

    verifier = TargetedVerifier(StaticModelProvider(response_factory)).verify(
        suspicious, validation, controls
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation, verifier)

    assert verifier.status == "partial"
    assert resolved[0].status == "pass"


def test_manual_required_closes_ledger_and_warns_by_policy(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    control = _control().model_copy(
        update={
            "severity": "medium",
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
            "missing_evidence_policy": "warn",
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    result = ReviewResult(
        contract="review_result.v1",
        work_item_id="wi.privacy.frontend_h5",
        attempt_id="attempt-manual",
        execution_status="completed",
        rows=[
            {
                "control_id": control.control_id,
                "surface": "frontend_h5",
                "evidence_status": "manual_required",
                "recommended_control_status": "indeterminate",
                "gap_reasons": ["Play Console declaration must be supplied manually."],
            }
        ],
        agent_id="reviewer-001",
    )
    summary = ReviewRunSummary(
        run_id="run-manual",
        executions=[
            WorkerExecution(
                work_item_id=result.work_item_id,
                attempt_id=result.attempt_id,
                agent_id=result.agent_id,
                execution_status="completed",
                result_path=(frontend / "result.json").as_posix(),
                result=result,
                context_fingerprint="manual",
            )
        ],
        max_concurrency=1,
        completed=1,
        failed=0,
        event_log_path=(frontend / "events.jsonl").as_posix(),
    )
    validation = ResultValidator().validate(
        summary,
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation, None)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved)

    assert validation.rows[0].valid is True
    assert resolved[0].status == "indeterminate"
    assert gate.complete is True
    assert gate.ci_status == "warn"
    assert gate.rows[0].result_origin == "manual_required"


def test_not_applicable_units_are_terminal_and_ci_neutral() -> None:
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        excluded_control_ids=[control.control_id],
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.frontend_h5",
                control_id=control.control_id,
                module_id="privacy",
                surface="frontend_h5",
                applicability_status="not_applicable",
                coverage_status="not_applicable",
                required_evidence_strength="static_proof",
                reason="fixture",
            )
        ],
    )
    validation = ResultValidator().validate(
        ReviewRunSummary(
            run_id="run-na",
            executions=[],
            max_concurrency=1,
            completed=0,
            failed=0,
            event_log_path="events.jsonl",
        ),
        coverage,
        controls,
        {},
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation, None)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved)

    assert resolved[0].status == "not_applicable"
    assert gate.ci_status == "pass"
    assert gate.complete is True
    assert gate.rows[0].result_origin == "not_applicable"
    assert gate.rows[0].execution_status == "not_required"
    assert gate.rows[0].evidence_status == "not_applicable"


def test_not_required_surface_is_terminal_without_fake_execution() -> None:
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version="1.0",
        control_version="1.0",
        units=[
            CoverageUnit(
                coverage_unit_id="cu.privacy.backend_required.frontend_h5",
                control_id=control.control_id,
                module_id="privacy",
                surface="frontend_h5",
                applicability_status="applicable",
                coverage_status="not_required",
                required_evidence_strength="static_proof",
                reason="The obligation does not require an H5 delivery surface.",
            )
        ],
    )
    validation = ResultValidator().validate(
        ReviewRunSummary(
            run_id="run-not-required",
            executions=[],
            max_concurrency=1,
            completed=0,
            failed=0,
            event_log_path="events.jsonl",
        ),
        coverage,
        controls,
        {},
    )
    resolved = ComplianceResolver().resolve(controls, coverage, validation, None)
    gate = CoverageGate().evaluate(controls, coverage, validation, resolved)

    assert validation.valid is True
    assert resolved[0].status == "indeterminate"
    assert "no_required_surface" in resolved[0].reasons[0]
    assert gate.ci_status == "block"
    assert gate.complete is True
    assert gate.rows[0].result_origin == "not_required"
    assert gate.rows[0].execution_status == "not_required"
    assert gate.rows[0].evidence_status == "not_required"


def test_reviewer_evidence_status_cannot_change_surface_applicability() -> None:
    with pytest.raises(ValidationError):
        ControlSurfaceResult(
            control_id="privacy.backend_required",
            surface="frontend_h5",
            evidence_status="not_required",  # type: ignore[arg-type]
            recommended_control_status="indeterminate",
        )


def test_explicit_fail_precedes_missing_other_surface(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    control = _control()
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    summary = _frontend_summary(frontend)
    result = summary.executions[0].result
    assert result is not None
    failed_row = result.rows[0].model_copy(update={"recommended_control_status": "fail"})
    result = result.model_copy(update={"rows": [failed_row]})
    summary = summary.model_copy(
        update={"executions": [summary.executions[0].model_copy(update={"result": result})]}
    )
    validation = ResultValidator().validate(
        summary,
        _coverage(),
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    resolved = ComplianceResolver().resolve(controls, _coverage(), validation, None)

    assert resolved[0].status == "fail"


def test_failed_verifier_cannot_confirm_a_suspicious_pass(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "consent.js").write_text("const consent = true;\n", encoding="utf-8")
    control = _control().model_copy(
        update={
            "required_surfaces": ["frontend_h5"],
            "minimum_evidence_strength": {"frontend_h5": "static_proof"},
        }
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    coverage = _coverage().model_copy(
        update={"units": [_coverage().units[0]], "missing_surfaces": []}
    )
    validation = ResultValidator().validate(
        _frontend_summary(frontend),
        coverage,
        controls,
        {"frontend_h5": RepositorySandbox(frontend)},
    )
    row_id = "privacy.backend_required:frontend_h5"
    failed_verifier = VerifierResult(
        status="failed",
        decisions=[
            VerifierDecision(
                row_id=row_id,
                decision="confirm",
                reason="This decision must be ignored because the verifier failed.",
            )
        ],
    )

    resolved = ComplianceResolver().resolve(controls, coverage, validation, failed_verifier)

    assert resolved[0].status == "pass"
