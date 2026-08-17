from __future__ import annotations

import json
from pathlib import Path

from compliance_review.compilation.models import ControlValidationResult
from compliance_review.domain.models import (
    ApplicabilityDecision,
    Control,
    ControlSet,
    CoverageGateResult,
    CoverageManifestRow,
    ResolvedControlResult,
    Snapshot,
    SourceRef,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.review import FullReviewService, LangGraphReviewRuntime
from compliance_review.review.full_review import render_markdown_report
from compliance_review.review.models import ModelRequest, ModelResponse, ToolCall
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.models import WorkspaceRepository
from compliance_review.setup.service import ReviewSetupService

FIXTURES = Path(__file__).parent / "fixtures" / "day2"


def test_full_review_writes_snapshot_and_blocks_missing_backend_evidence(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    setup_service = ReviewSetupService(workspace_root)
    initialized = setup_service.initialize(
        [
            WorkspaceRepository(
                repo_id="frontend",
                path=(FIXTURES / "frontend").as_posix(),
                declared_surface="frontend_h5",
            )
        ]
    )
    setup_service.confirm_profile(
        {
            "app_name": "Example Loan",
            "package_name": "com.example.loan",
            "jurisdiction": "Pakistan",
            "business_type": ["personal_loan"],
            "self_lending": True,
        }
    )
    control = Control(
        control_id="privacy.backend_required",
        module_id="privacy",
        title="Backend implementation must support the disclosure",
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
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    store = ArtifactStore(workspace_root)
    store.write_controls(controls)
    store.write_control_validation(ControlValidationResult(valid=True, validated_control_count=1))
    setup = setup_service.compile(
        initialized.workspace, run_id="run-full-review", max_concurrency=3
    )

    review_calls: dict[str, int] = {}
    verification_calls = 0

    def response_factory(request: ModelRequest) -> ModelResponse:
        nonlocal verification_calls
        if request.request_kind == "verification":
            verification_calls += 1
            raise AssertionError("authoritative Full Review must not call verifier")
        count = review_calls.get(request.attempt_id, 0)
        review_calls[request.attempt_id] = count + 1
        if count == 0:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="read-router",
                        name="read_file",
                        arguments={
                            "path": "src/router.js",
                            "start_line": 1,
                            "line_count": 2,
                        },
                    )
                ]
            )
        ledger_message = next(
            message["content"]
            for message in request.messages
            if str(message.get("content", "")).startswith("Durable evidence ledger:")
        )
        anchor_id = json.loads(ledger_message.split("\n", 1)[1])[0]["anchor_id"]
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "review_result.v1",
                    "work_item_id": request.work_item.work_item_id,
                    "attempt_id": request.attempt_id,
                    "execution_status": "completed",
                    "rows": [
                        {
                            "control_id": "privacy.backend_required",
                            "surface": "frontend_h5",
                            "evidence_status": "complete",
                            "recommended_control_status": "pass",
                            "observed_evidence_strength": "static_proof",
                            "anchor_ids": [anchor_id],
                            "confidence": "high",
                        }
                    ],
                    "agent_id": request.agent_id,
                }
            )
        )

    provider = StaticModelProvider(response_factory)
    result = FullReviewService(
        workspace_root,
        LangGraphReviewRuntime(provider=provider, max_concurrency=3),
        verifier_provider=provider,
    ).run(setup, controls)

    assert result.resolved_controls[0].status == "indeterminate"
    assert result.coverage_gate.ci_status == "block"
    assert result.snapshot.ci_status == "block"
    run_root = workspace_root / "runs" / "run-full-review"
    for relative in (
        "result_validation.json",
        "validation_flags.json",
        "control_results.json",
        "coverage_manifest.json",
        "snapshot.json",
        "report.md",
    ):
        assert (run_root / relative).is_file()
    stored_snapshot = Snapshot.model_validate_json(
        (run_root / "snapshot.json").read_text(encoding="utf-8")
    )
    assert verification_calls == 0
    assert not (run_root / "suspicious_rows.json").exists()
    assert not (run_root / "verifier" / "verifier_result.json").exists()
    stored_gate = CoverageGateResult.model_validate_json(
        (run_root / "coverage_manifest.json").read_text(encoding="utf-8")
    )
    report = (run_root / "report.md").read_text(encoding="utf-8")
    assert report == render_markdown_report(stored_snapshot, stored_gate)
    assert "# 金融应用合规审查报告" in report
    assert "Reviewer WorkItem 完成 / 失败" in report
    assert "已验证完整证据单元" in report
    assert "| CI 判定 | **阻断** |" in report
    assert "## 证据覆盖台账" in report
    assert "backend_code" in report
    assert "Consent setting" not in report
    assert stored_snapshot.reviewed_rows == ["cu.privacy.backend_required.frontend_h5"]


def test_report_distinguishes_completed_work_from_complete_evidence_and_records_applicability() -> (
    None
):
    snapshot = Snapshot(
        contract="compliance_snapshot.v1",
        run_id="report-fixture",
        git_revision="abc123",
        mode="full",
        control_results=[
            ResolvedControlResult(
                control_id="control-0007",
                status="indeterminate",
                severity="high",
                reasons=["Validated evidence is incomplete or invalid."],
            )
        ],
        coverage_manifest_ref="coverage_manifest.json",
        applicability_hash="applicability-hash",
        ci_status="block",
        reviewed_rows=[],
        reviewed_partial_rows=["cu.control-0007.frontend_h5"],
        reviewer_work_items_completed=1,
        reviewer_work_items_failed=0,
        applicability_decisions=[
            ApplicabilityDecision(
                control_id="control-0007",
                decision="unknown",
                reason="Profile fact still requires human confirmation.",
                unresolved_conditions=["licensed_entity_name"],
                confidence="low",
            )
        ],
        missing_surfaces=["other_external"],
        run_status="completed",
    )
    gate = CoverageGateResult(
        complete=False,
        ci_status="block",
        rows=[
            CoverageManifestRow(
                coverage_unit_id="cu.control-0007.frontend_h5",
                control_id="control-0007",
                surface="frontend_h5",
                work_item_id="wi.control-0007",
                execution_status="completed",
                evidence_status="partial",
                result_origin="blocked",
                coverage_reason=(
                    "reviewer result failed deterministic validation Required evidence rationale: "
                    "Registration consent must be explicit before account creation."
                ),
                resolution_status="indeterminate",
            )
        ],
        blocking_reasons=["coverage_incomplete"],
    )

    report = render_markdown_report(snapshot, gate)

    assert "| Reviewer WorkItem 完成 / 失败 | `1` / `0` |" in report
    assert "| 已验证完整证据单元 | `0` |" in report
    assert "| 已执行但证据未完整单元 | `1` |" in report
    assert "| `cu.control-0007.frontend_h5` | H5 / WebView | 已执行 | 部分 |" in report
    assert "未形成有效自动化证据" in report
    assert "## 适用性台账" in report
    assert "未知（保守保留）" in report
    assert "## 阻断原因" in report

    missing_report = render_markdown_report(
        snapshot,
        gate.model_copy(
            update={
                "rows": [
                    gate.rows[0].model_copy(
                        update={"evidence_status": "missing"}
                    )
                ]
            }
        ),
    )
    assert "| `cu.control-0007.frontend_h5` | H5 / WebView | 已执行 | 缺失 |" in missing_report

    failed_report = render_markdown_report(
        snapshot,
        gate.model_copy(
            update={
                "rows": [
                    gate.rows[0].model_copy(update={"execution_status": "failed"})
                ]
            }
        ),
    )
    assert "| `cu.control-0007.frontend_h5` | H5 / WebView | 执行失败 | 部分 |" in failed_report

    pending_report = render_markdown_report(
        snapshot,
        gate.model_copy(
            update={
                "rows": [
                    gate.rows[0].model_copy(update={"execution_status": "pending"})
                ]
            }
        ),
    )
    assert "| `cu.control-0007.frontend_h5` | H5 / WebView | 未执行 | 部分 |" in pending_report
