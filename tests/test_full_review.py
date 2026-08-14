from __future__ import annotations

import json
from pathlib import Path

from compliance_review.compilation.models import ControlValidationResult
from compliance_review.domain.models import (
    Control,
    ControlSet,
    CoverageGateResult,
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
    assert "CI decision: **BLOCK**" in report
    assert "backend_code" in report
    assert "Consent setting" not in report
    assert stored_snapshot.reviewed_rows == ["cu.privacy.backend_required.frontend_h5"]
