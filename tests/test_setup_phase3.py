from __future__ import annotations

import json
from pathlib import Path

import pytest

from compliance_review.collectors.api_documents import ApiDocumentCollector
from compliance_review.compilation.models import ControlValidationResult
from compliance_review.config.loader import load_controls
from compliance_review.domain.models import (
    ApplicabilityCondition,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    ApplicabilitySet,
    Control,
    ControlSet,
    CoverageSet,
    CoverageUnit,
    EvidenceRequirement,
    ProfileFactRef,
    SourceRef,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.repository import RepositorySandbox
from compliance_review.review import LangGraphReviewRuntime
from compliance_review.review.models import ModelRequest, ModelResponse
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup import ReviewSetupError, ReviewSetupService
from compliance_review.setup.app_facts import collect_app_facts
from compliance_review.setup.models import (
    AppFactSet,
    RepositoryInventory,
    WorkspaceMaterial,
    WorkspaceRepository,
)
from compliance_review.setup.planning import CoverageUnitBuilder, WorkItemPlanner

FIXTURES = Path(__file__).parent / "fixtures" / "day2"
PROJECT_ROOT = Path(__file__).parents[1]


def test_api_document_material_is_collected_with_provenance(tmp_path: Path) -> None:
    api_doc = tmp_path / "fineract.json"
    api_doc.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/v1/loans": {
                        "post": {"operationId": "createLoan"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    material = WorkspaceMaterial(
        path=api_doc.as_posix(),
        source_family="backend_api_doc",
        surface="backend_api_doc",
        provenance={"kind": "official_generated_openapi"},
        limitations=["not a runtime authorization proof"],
    )

    facts = collect_app_facts([], [material])

    assert len(facts.facts) == 1
    assert facts.facts[0].source_surface == "backend_api_doc"
    assert facts.facts[0].repo_id == "workspace"
    assert facts.facts[0].source_refs[0].path == api_doc.as_posix()
    assert facts.collector_results[0]["metadata"]["provenance"] == material.provenance
    assert "not a runtime authorization proof" in facts.facts[0].limitations


def test_large_api_document_uses_bounded_collector_budget(tmp_path: Path) -> None:
    api_doc = tmp_path / "large-fineract.json"
    api_doc.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "Large API", "description": "x" * 1_100_000},
                "paths": {"/v1/loans": {"get": {"operationId": "listLoans"}}},
            }
        ),
        encoding="utf-8",
    )
    assert api_doc.stat().st_size > 1_000_000

    result = ApiDocumentCollector().collect(
        RepositorySandbox(api_doc.parent), roots=(".",), file_globs=(api_doc.name,)
    )

    assert result.parser_status == "ok"
    assert result.coverage_status == "complete"
    assert result.metadata["endpoint_count"] == 1


def _prepare_workspace(tmp_path: Path) -> tuple[ReviewSetupService, object, ControlSet]:
    workspace_root = tmp_path / "workspace"
    def applicability_response(request: ModelRequest) -> ModelResponse:
        payload = json.loads(request.messages[1]["content"])
        control = payload["control"]
        def condition_fact_keys(condition: dict[str, object]) -> list[str]:
            if condition.get("kind") == "atom":
                fact = condition.get("fact")
                return [fact] if isinstance(fact, str) else []
            return [
                fact_key
                for child in condition.get("conditions", [])
                if isinstance(child, dict)
                for fact_key in condition_fact_keys(child)
            ]

        profile_fact_refs = []
        for fact_key in condition_fact_keys(control["applicability_condition"]):
            fact = payload["confirmed_profile_facts"].get(fact_key)
            if fact is not None and fact["source"] in {"human_confirmed", "deterministic"}:
                profile_fact_refs.append(
                    {
                        "field_name": fact_key,
                        "expected_value": json.dumps(
                            fact["value"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ),
                    }
                )
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control["control_id"],
                    "decision": "applicable",
                    "reason": "Fixture explicitly supplies an applicable control.",
                    "profile_fact_refs": profile_fact_refs,
                    "confidence": "high",
                }
            )
        )

    service = ReviewSetupService(
        workspace_root,
        applicability_provider=StaticModelProvider(applicability_response),
    )
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


def test_phase3_blocks_without_validated_controls_but_not_profile_questions(tmp_path: Path) -> None:
    service = ReviewSetupService(tmp_path / "workspace")
    service.initialize(
        [WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())]
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
    assert len(result.work_items) == sum(
        unit.work_item_id is not None for unit in result.coverage.units
    )
    assert all(
        len(item.control_ids) == 1 and len(item.coverage_unit_ids) == 1
        for item in result.work_items
    )
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
    checkpoint_path = (
        service.workspace_root / "setup" / "applicability_resolution_checkpoint.json"
    )
    assert checkpoint_path.is_file()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["contract"] == "applicability_resolution_checkpoint.v1"
    assert len(checkpoint["decisions"]) == len(controls.controls)
    assert len(result.manifest.coverage_unit_ids) == len(result.coverage.units)
    # Candidate surfaces absent from the confirmed profile are not synthetic
    # coverage gaps. A user can explicitly configure a missing surface when it
    # should block the review.
    assert not any(unit.coverage_status == "missing_surface" for unit in result.coverage.units)
    assert all(
        unit.coverage_status in {"planned", "unknown_applicability"}
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


def test_phase3_uses_resolution_loop_and_retains_unknown_for_bounded_review(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    call_count = 0

    def applicability_response(request: ModelRequest) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        payload = json.loads(request.messages[1]["content"])
        discovered = payload["confirmed_profile_facts"].get(
            "sensitive_permission_use_present"
        )
        if discovered is None:
            decision = {
                "control_id": "control.sensitive-permissions",
                "decision": "unknown",
                "reason": "Permission usage must be discovered from the Android repository.",
                "unresolved_conditions": [
                    "Whether sensitive permissions are used is unconfirmed."
                ],
                "confidence": "low",
            }
        else:
            decision = {
                "control_id": "control.sensitive-permissions",
                "decision": "applicable",
                "reason": "A deterministic Android permission fact is confirmed.",
                "unresolved_conditions": [],
                "confidence": "high",
            }
        return ModelResponse(content=json.dumps({"decisions": [decision]}))

    service = ReviewSetupService(
        workspace_root,
        applicability_provider=StaticModelProvider(applicability_response),
    )
    service.initialize(
        [
            WorkspaceRepository(
                repo_id="android",
                path=(FIXTURES / "android").as_posix(),
                declared_surface="android_native",
            )
        ]
    )
    service.confirm_profile(
        {
            "app_name": "Example",
            "package_name": "com.example",
            "jurisdiction": "Pakistan",
            "business_type": ["banking"],
            "self_lending": False,
        },
        repository_surfaces={"android": "android_native"},
    )
    control = Control(
        control_id="control.sensitive-permissions",
        module_id="privacy",
        title="Sensitive permission usage",
        severity="high",
        applicability_condition=ApplicabilityCondition(
            kind="atom",
            fact="sensitive_permission_use_present",
            operator="equals",
            value=True,
        ),
        required_surfaces=["android_native"],
        minimum_evidence_strength={"android_native": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[{"url": "https://example.test/policy"}],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "android_native": EvidenceRequirement(
                minimum_strength="static_proof",
                rationale="Inspect permission declarations and use paths.",
            )
        },
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    store = ArtifactStore(workspace_root)
    store.write_controls(controls)
    store.write_control_validation(
        ControlValidationResult(valid=True, validated_control_count=1)
    )

    result = service.compile(run_id="discovery-recheck")

    assert call_count == 1
    assert result.applicability is not None
    assert result.applicability.decisions[0].decision == "unknown"
    assert result.coverage is not None
    assert result.coverage.units[0].coverage_status == "unknown_applicability"
    assert len(result.work_items) == 1
    assert result.applicability_resolution is not None
    assert result.applicability_resolution.status == "awaiting_human"
    assert (workspace_root / "setup" / "applicability_resolution.json").is_file()
    assert not (workspace_root / "setup" / "applicability_discovery_results.json").exists()


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


def test_unknown_applicability_is_retained_and_scheduled_for_bounded_review(
    tmp_path: Path,
) -> None:
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

    from compliance_review.setup.planning import (
        ApplicabilityEngine,
        CoverageUnitBuilder,
        WorkItemPlanner,
    )

    applicability = ApplicabilityEngine().evaluate(profile, controls)
    coverage = CoverageUnitBuilder().build(profile, controls, applicability)
    inventory = RepositoryInventory(
        repo_id="web",
        path=(FIXTURES / "frontend").as_posix(),
        detected_surface="frontend_h5",
        detected_surfaces=["frontend_h5"],
        surface_status="confirmed",
    )
    plan = WorkItemPlanner().plan(
        profile,
        controls,
        coverage,
        AppFactSet(inventory_ids=["web"]),
        [inventory],
        tmp_path / "run",
    )

    assert applicability.unknown_control_ids == ["control.unknown"]
    assert len(coverage.units) == 1
    assert coverage.units[0].coverage_status == "unknown_applicability"
    assert len(plan.work_items) == 1
    assert plan.work_items[0].control_ids == [control.control_id]
    assert plan.work_items[0].coverage_unit_ids == [coverage.units[0].coverage_unit_id]
    assert plan.work_items[0].target_hints["coverage_status"] == [
        "unknown_applicability"
    ]


def test_external_surface_uses_registered_material_without_surface_hardcoding(
    tmp_path: Path,
) -> None:
    material_root = tmp_path / "play-console"
    material_root.mkdir()
    (material_root / "listing.md").write_text("Test-only Play Console listing", encoding="utf-8")
    manifest_path = material_root / "external_materials_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "material_status": "verified_external_materials",
                "not_validated_as_official": False,
                "materials": [
                    {
                        "surface": "play_console",
                        "verification_status": "verified_by_owner",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    profile = _applicability_profile_for_test(
        evidence_surfaces=["play_console"],
        roots={"play_console": material_root.as_posix()},
    )
    control = Control(
        control_id="control.play-material",
        module_id="listing",
        title="Store listing disclosure",
        severity="high",
        applicability_expression="unknown",
        required_surfaces=["play_console"],
        minimum_evidence_strength={"play_console": "declared"},
        missing_evidence_policy="block",
        source_refs=[{"url": "https://example.test/policy"}],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "play_console": EvidenceRequirement(
                minimum_strength="declared", rationale="The listing must be checked."
            )
        },
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    applicability = ApplicabilitySet(
        profile_version=profile.version,
        control_version=controls.version,
        decisions=[
            {
                "control_id": control.control_id,
                "decision": "applicable",
                "reason": "The fixture requires listing evidence.",
                "surface_requirements": [
                    {
                        "surface": "play_console",
                        "decision": "required",
                        "reason": "The listing is an in-scope evidence source.",
                    }
                ],
                "resolved_required_surfaces": ["play_console"],
            }
        ],
    )
    coverage = CoverageUnitBuilder().build(
        profile, controls, applicability, available_surfaces={"play_console"}
    )
    plan = WorkItemPlanner().plan(
        profile,
        controls,
        coverage,
        AppFactSet(inventory_ids=[]),
        [],
        tmp_path / "run",
        materials=[
            WorkspaceMaterial(
                path=material_root.as_posix(),
                source_family="google_play",
                surface="play_console",
            )
        ],
    )

    assert coverage.units[0].coverage_status == "planned"
    assert len(plan.work_items) == 1
    assert plan.work_items[0].surface == "play_console"
    assert plan.work_items[0].target_hints["evidence_source_kind"] == [
        "workspace_material"
    ]
    assert plan.work_items[0].external_evidence_policy == "strict"

    trusted_plan = WorkItemPlanner().plan(
        profile,
        controls,
        coverage,
        AppFactSet(inventory_ids=[]),
        [],
        tmp_path / "run-trusted",
        materials=[
            WorkspaceMaterial(
                path=material_root.as_posix(),
                source_family="google_play",
                surface="play_console",
            ),
            WorkspaceMaterial(
                path=manifest_path.as_posix(),
                source_family="internal",
                surface="play_console",
            ),
        ],
        external_evidence_policy="trusted_test_materials",
    )
    assert trusted_plan.work_items[0].external_evidence_policy == "trusted_test_materials"

    unavailable_coverage = CoverageUnitBuilder().build(
        profile, controls, applicability, available_surfaces=set()
    )
    unavailable_plan = WorkItemPlanner().plan(
        profile,
        controls,
        unavailable_coverage,
        AppFactSet(inventory_ids=[]),
        [],
        tmp_path / "run-unavailable",
    )
    assert unavailable_coverage.units[0].coverage_status == "missing_surface"
    assert unavailable_plan.work_items == []


def test_claim_routes_on_same_control_surface_get_unique_work_item_ids(
    tmp_path: Path,
) -> None:
    profile = _applicability_profile_for_test(
        evidence_surfaces=["frontend_h5"],
        roots={"frontend_h5": (FIXTURES / "frontend").as_posix()},
    )
    control = Control(
        control_id="control.multi-claim",
        module_id="disclosure",
        title="Multiple claims on one surface",
        severity="high",
        applicability_expression="unknown",
        required_surfaces=["frontend_h5"],
        minimum_evidence_strength={"frontend_h5": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[{"url": "https://example.test/policy"}],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "frontend_h5": EvidenceRequirement(
                minimum_strength="static_proof", rationale="Inspect both claims."
            )
        },
    )
    controls = ControlSet(contract="control_set.v2", version="1.0", controls=[control])
    coverage = CoverageSet(
        profile_version=profile.version,
        control_version=controls.version,
        units=[
            CoverageUnit(
                coverage_unit_id="cu.control.multi-claim.claim-a.route-a",
                control_id=control.control_id,
                module_id=control.module_id,
                surface="frontend_h5",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="static_proof",
                reason="route selected",
                claim_id="claim-a",
                route_id="route-a",
            ),
            CoverageUnit(
                coverage_unit_id="cu.control.multi-claim.claim-b.route-b",
                control_id=control.control_id,
                module_id=control.module_id,
                surface="frontend_h5",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="static_proof",
                reason="route selected",
                claim_id="claim-b",
                route_id="route-b",
            ),
        ],
    )
    inventory = RepositoryInventory(
        repo_id="web",
        path=(FIXTURES / "frontend").as_posix(),
        detected_surface="frontend_h5",
        detected_surfaces=["frontend_h5"],
        surface_status="confirmed",
    )

    plan = WorkItemPlanner().plan(
        profile,
        controls,
        coverage,
        AppFactSet(inventory_ids=["web"]),
        [inventory],
        tmp_path / "run",
    )

    assert len(plan.work_items) == 2
    assert len({item.work_item_id for item in plan.work_items}) == 2
    assert {item.coverage_unit_id for item in plan.work_items} == {
        unit.coverage_unit_id for unit in coverage.units
    }


def _applicability_profile_for_test(
    evidence_surfaces: list[str], roots: dict[str, str]
) -> ApplicabilityProfile:
    return ApplicabilityProfile(
        contract="applicability_profile.v1",
        version="1.0",
        app_name="Example",
        package_name="com.example",
        jurisdiction="Pakistan",
        business_type=["personal_loan"],
        self_lending=True,
        evidence_surfaces=evidence_surfaces,
        review_scope="multi_surface_static_review",
        roots=roots,
    )


def test_semantic_not_applicable_requires_real_sources_and_human_profile_facts() -> None:
    profile = ApplicabilityProfile(
        contract="applicability_profile.v1",
        version="1.0",
        app_name="Example",
        package_name="com.example",
        jurisdiction="Pakistan",
        business_type=["personal_loan"],
        self_lending=True,
        evidence_surfaces=["frontend_h5"],
        review_scope="multi_surface_static_review",
        roots={"frontend_h5": "."},
        confirmed_facts={
            "business_type": ApplicabilityProfileFact(
                value=["personal_loan"], source="human_confirmed"
            )
        },
    )
    source_ref = SourceRef(source_id="policy-1", source_section="section-1")
    control = Control(
        control_id="control.semantic",
        module_id="privacy",
        title="Semantic applicability fixture",
        severity="high",
        applicability_expression="unknown",
        required_surfaces=["frontend_h5"],
        minimum_evidence_strength={"frontend_h5": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[source_ref],
        reuse_invalidation_keys=["control_version"],
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])

    def response_factory(request: ModelRequest) -> ModelResponse:
        assert request.request_kind == "applicability"
        assert request.tools == []
        return ModelResponse(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "control_id": control.control_id,
                            "decision": "not_applicable",
                            "reason": "The confirmed profile is a personal-loan product.",
                            "source_refs": [source_ref.model_dump(mode="json")],
                            "profile_fact_refs": [
                                ProfileFactRef(
                                    field_name="business_type",
                                    expected_value='["personal_loan"]',
                                ).model_dump(mode="json")
                            ],
                            "unresolved_conditions": [],
                            "confidence": "high",
                        }
                    ]
                }
            )
        )

    from compliance_review.setup.planning import ApplicabilityEngine, CoverageUnitBuilder

    engine = ApplicabilityEngine(StaticModelProvider(response_factory))
    applicability = engine.evaluate(profile, controls)
    coverage = CoverageUnitBuilder().build(profile, controls, applicability)

    assert applicability.excluded_control_ids == [control.control_id]
    assert applicability.decisions[0].decision == "not_applicable"
    assert coverage.units[0].coverage_status == "not_applicable"


def test_unverified_semantic_exclusion_downgrades_to_unknown_and_is_not_dropped() -> None:
    profile = ApplicabilityProfile(
        contract="applicability_profile.v1",
        version="1.0",
        app_name="Example",
        package_name="com.example",
        jurisdiction="Pakistan",
        business_type=["personal_loan"],
        self_lending=True,
        evidence_surfaces=["frontend_h5"],
        review_scope="multi_surface_static_review",
        roots={"frontend_h5": "."},
        confirmed_facts={
            "business_type": ApplicabilityProfileFact(
                value=["personal_loan"], source="human_confirmed"
            )
        },
    )
    control = Control(
        control_id="control.unverified-exclusion",
        module_id="privacy",
        title="Unverified exclusion fixture",
        severity="high",
        applicability_expression="unknown",
        required_surfaces=["frontend_h5"],
        minimum_evidence_strength={"frontend_h5": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[SourceRef(source_id="policy-1", source_section="section-1")],
        reuse_invalidation_keys=["control_version"],
    )
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])

    def response_factory(_: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "control_id": control.control_id,
                            "decision": "not_applicable",
                            "reason": "Unsupported exclusion.",
                            "source_refs": [{"source_id": "invented", "source_section": "x"}],
                            "profile_fact_refs": [],
                            "unresolved_conditions": [],
                            "confidence": "high",
                        }
                    ]
                }
            )
        )

    from compliance_review.setup.planning import ApplicabilityEngine, CoverageUnitBuilder

    engine = ApplicabilityEngine(StaticModelProvider(response_factory))
    applicability = engine.evaluate(profile, controls)
    coverage = CoverageUnitBuilder().build(profile, controls, applicability)

    assert applicability.excluded_control_ids == []
    assert applicability.unknown_control_ids == [control.control_id]
    assert applicability.decisions[0].decision == "unknown"
    assert "unverified_not_applicable" in applicability.decisions[0].unresolved_conditions
    assert coverage.units[0].coverage_status == "unknown_applicability"
