from __future__ import annotations

import json
from pathlib import Path

import pytest

from compliance_review.compilation.models import ControlValidationResult
from compliance_review.config.loader import load_controls
from compliance_review.domain.models import (
    ApplicabilityProfile,
    Control,
    ControlSet,
    EvidenceRequirement,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.review import LangGraphReviewRuntime
from compliance_review.review.models import ModelRequest, ModelResponse
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup import ReviewSetupError, ReviewSetupService
from compliance_review.setup.models import WorkspaceRepository

FIXTURES = Path(__file__).parent / "fixtures" / "day2"
PROJECT_ROOT = Path(__file__).parents[1]


def _prepare_workspace(tmp_path: Path) -> tuple[ReviewSetupService, object, ControlSet]:
    workspace_root = tmp_path / "workspace"
    service = ReviewSetupService(workspace_root)
    phase1 = service.initialize(
        [WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())]
    )
    service.confirm_profile(
        {
            "app_name": "Example Loan",
            "package_name": "com.example.loan",
            "jurisdiction": "Pakistan",
            "business_type": ["personal_loan"],
            "self_lending": True,
        }
    )
    controls = load_controls(PROJECT_ROOT / "examples/mvp-controls.yaml")
    store = ArtifactStore(workspace_root)
    store.write_controls(controls)
    store.write_control_validation(
        ControlValidationResult(
            valid=True,
            validated_control_count=len(controls.controls),
        )
    )
    return service, phase1.workspace, controls


def test_phase3_blocks_without_confirmed_profile_or_validated_controls(tmp_path: Path) -> None:
    service = ReviewSetupService(tmp_path / "workspace")
    service.initialize(
        [WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())]
    )

    with pytest.raises(ReviewSetupError, match="confirmed AppProfile"):
        service.compile()

    service.confirm_profile(
        {
            "app_name": "Example Loan",
            "package_name": "com.example.loan",
            "jurisdiction": "Pakistan",
            "business_type": ["personal_loan"],
            "self_lending": True,
        }
    )
    with pytest.raises(ReviewSetupError, match="validated controls"):
        service.compile()


def test_phase3_builds_applicability_coverage_and_runtime_handoff(tmp_path: Path) -> None:
    service, workspace, controls = _prepare_workspace(tmp_path)

    result = service.compile(workspace, run_id="run-phase3", max_concurrency=3)

    assert result.run_id == "run-phase3"
    assert result.applicability is not None
    assert result.coverage is not None
    assert result.manifest is not None
    assert result.work_items
    assert result.sandboxes
    assert len(result.coverage.units) == sum(
        len(control.required_surfaces) for control in controls.controls
    )
    assert all(
        unit.coverage_status == "not_applicable"
        for unit in result.coverage.units
        if unit.control_id in result.coverage.excluded_control_ids
    )
    assert all(
        unit.control_id not in result.coverage.excluded_control_ids
        for item in result.work_items
        for unit in result.coverage.units
        if unit.coverage_unit_id in item.coverage_unit_ids
    )
    assert len(result.manifest.coverage_unit_ids) == len(result.coverage.units)
    assert any(unit.coverage_status == "missing_surface" for unit in result.coverage.units)
    assert all(
        unit.coverage_status == "planned"
        for item in result.work_items
        for unit in result.coverage.units
        if unit.coverage_unit_id in item.coverage_unit_ids
    )
    assert all(
        item.collector_fact_refs for item in result.work_items if item.surface == "frontend_h5"
    )

    run_root = tmp_path / "workspace" / "runs" / "run-phase3"
    assert (tmp_path / "workspace" / "setup" / "applicability.json").is_file()
    assert (tmp_path / "workspace" / "setup" / "coverage_units.json").is_file()
    assert (run_root / "manifest.json").is_file()
    assert (run_root / "reviewer_results").is_dir()
    assert (run_root / "worker-events.jsonl").is_file()
    assert (run_root / "checkpoint.sqlite").is_file()

    def response_factory(request: ModelRequest) -> ModelResponse:
        payload = {
            "contract": "review_result.v1",
            "work_item_id": request.work_item.work_item_id,
            "attempt_id": request.attempt_id,
            "execution_status": "completed",
            "rows": [
                {
                    "control_id": control_id,
                    "surface": request.work_item.surface,
                    "evidence_status": "missing",
                    "recommended_control_status": "indeterminate",
                    "gap_reasons": ["fixture runtime handoff"],
                }
                for control_id in request.work_item.control_ids
            ],
            "agent_id": request.agent_id,
        }
        return ModelResponse(content=json.dumps(payload))

    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(response_factory),
        max_concurrency=3,
    ).run(
        manifest_run_id=result.run_id or "",
        work_items=result.work_items,
        sandboxes=result.sandboxes,
        output_root=run_root / "reviewer_results",
        event_log_path=run_root / "worker-events.jsonl",
    )
    assert summary.completed == len(result.work_items)
    assert summary.failed == 0


def test_phase3_preserves_same_surface_repositories_and_fact_capabilities(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        (root / "package.json").write_text('{"dependencies":{"vue":"3"}}', encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    service = ReviewSetupService(workspace_root)
    service.initialize(
        [
            WorkspaceRepository(repo_id="first", path=str(first)),
            WorkspaceRepository(repo_id="second", path=str(second)),
        ]
    )
    service.confirm_profile(
        {
            "app_name": "Example",
            "package_name": "com.example",
            "jurisdiction": "Pakistan",
            "business_type": ["personal_loan"],
            "self_lending": True,
        }
    )
    controls = load_controls(PROJECT_ROOT / "examples/mvp-controls.yaml")
    store = ArtifactStore(workspace_root)
    store.write_controls(controls)
    store.write_control_validation(
        ControlValidationResult(valid=True, validated_control_count=len(controls.controls))
    )

    result = service.compile(run_id="same-surface")

    frontend_items = [item for item in result.work_items if item.surface == "frontend_h5"]
    assert frontend_items
    assert all(item.repository_id == "workspace" for item in frontend_items)
    assert all(set(item.repository_ids) == {"first", "second"} for item in frontend_items)
    assert all(item.collector_fact_refs for item in frontend_items)
    assert all(
        any(fact_id.startswith("fact.first.") for fact_id in item.collector_fact_refs)
        and any(fact_id.startswith("fact.second.") for fact_id in item.collector_fact_refs)
        for item in frontend_items
    )


def test_unknown_applicability_is_retained_as_unknown_coverage() -> None:
    profile = ApplicabilityProfile(
        contract="applicability_profile.v1",
        version="1.0",
        app_name="Example",
        package_name="com.example",
        jurisdiction="Pakistan",
        business_type=["personal_loan"],
        self_lending="unknown",
        evidence_surfaces=["frontend_h5"],
        review_scope="multi_surface_static_review",
        roots={"frontend_h5": "."},
    )
    control = Control(
        control_id="control.unknown",
        module_id="privacy",
        title="Unknown applicability fixture",
        severity="high",
        applicability_expression="self_lending == true",
        required_surfaces=["frontend_h5"],
        minimum_evidence_strength={"frontend_h5": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[{"url": "https://example.test/policy"}],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "frontend_h5": EvidenceRequirement(minimum_strength="static_proof", rationale="fixture")
        },
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])

    from compliance_review.setup.planning import ApplicabilityEngine, CoverageUnitBuilder

    applicability = ApplicabilityEngine().evaluate(profile, controls)
    coverage = CoverageUnitBuilder().build(profile, controls, applicability)

    assert applicability.unknown_control_ids == ["control.unknown"]
    assert len(coverage.units) == 1
    assert coverage.units[0].coverage_status == "unknown_applicability"
