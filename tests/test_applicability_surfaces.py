from __future__ import annotations

import json

import pytest

from compliance_review.compilation.models import (
    ComplianceSource,
    Obligation,
    SourceRegistry,
    SourceSection,
)
from compliance_review.domain.models import (
    AcceptanceCriterion,
    ApplicabilityCondition,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    ApplicabilitySet,
    Control,
    ControlSet,
    EvidenceClaim,
    EvidenceProofRoute,
    EvidenceRequirement,
    ProfileFactRef,
    SourceRef,
    SurfaceRequirementDecision,
)
from compliance_review.review.applicability import (
    SemanticApplicabilityEvaluator,
)
from compliance_review.review.models import ModelRequest, ModelResponse
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.planning import ApplicabilityEngine, CoverageUnitBuilder

SOURCE_REF = SourceRef(source_id="policy-1", source_section="section-1")


def _profile(surfaces: list[str]) -> ApplicabilityProfile:
    return ApplicabilityProfile(
        contract="applicability_profile.v1",
        version="1.0",
        app_name="Example Loan",
        package_name="com.example.loan",
        jurisdiction="Pakistan",
        business_type=["personal_loan"],
        self_lending=True,
        evidence_surfaces=surfaces,
        review_scope="multi_surface_static_review",
        roots={surface: "." for surface in surfaces},
        confirmed_facts={
            "evidence_surfaces": ApplicabilityProfileFact(
                value=surfaces, source="human_confirmed"
            )
        },
    )


def _control() -> Control:
    return Control(
        control_id="control.disclosure",
        module_id="loan_disclosure",
        title="User-facing disclosure",
        severity="high",
        applicability_expression="unknown",
        required_surfaces=["android_native", "frontend_h5"],
        minimum_evidence_strength={
            "android_native": "static_proof",
            "frontend_h5": "static_proof",
        },
        missing_evidence_policy="block",
        source_refs=[SOURCE_REF],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "android_native": EvidenceRequirement(
                minimum_strength="static_proof", rationale="Disclosure must be visible."
            ),
            "frontend_h5": EvidenceRequirement(
                minimum_strength="static_proof", rationale="Disclosure must be visible."
            ),
        },
    )


def _provider(surface_requirements: list[dict[str, object]]) -> StaticModelProvider:
    control = _control()

    def response_factory(_: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "control_id": control.control_id,
                            "decision": "applicable",
                            "reason": "The control applies to the confirmed product.",
                            "source_refs": [SOURCE_REF.model_dump(mode="json")],
                            "profile_fact_refs": [],
                            "surface_requirements": surface_requirements,
                            "unresolved_conditions": [],
                            "confidence": "high",
                        }
                    ]
                }
            )
        )

    return StaticModelProvider(response_factory)


def _requirement(surface: str) -> dict[str, object]:
    return SurfaceRequirementDecision(
        surface=surface,
        decision="required",
        reason="The obligation is delivered on this surface.",
        source_refs=[SOURCE_REF],
    ).model_dump(mode="json")


def _not_required(surface: str) -> dict[str, object]:
    return SurfaceRequirementDecision(
        surface=surface,
        decision="not_required",
        reason="The confirmed app does not offer this delivery surface.",
        source_refs=[SOURCE_REF],
        profile_fact_refs=[
            ProfileFactRef(
                field_name="evidence_surfaces", expected_value='["android_native"]'
            )
        ],
    ).model_dump(mode="json")


def _compile(profile: ApplicabilityProfile, requirements: list[dict[str, object]]):
    control = _control()
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    applicability = ApplicabilityEngine(_provider(requirements)).evaluate(profile, controls)
    coverage = CoverageUnitBuilder().build(profile, controls, applicability)
    return applicability, coverage


def test_android_only_profile_marks_absent_h5_candidate_not_required() -> None:
    applicability, coverage = _compile(
        _profile(["android_native"]),
        [_requirement("android_native"), _not_required("frontend_h5")],
    )

    by_surface = {unit.surface: unit for unit in coverage.units}
    assert applicability.decisions[0].decision == "applicable"
    assert by_surface["android_native"].coverage_status == "planned"
    assert by_surface["frontend_h5"].coverage_status == "not_required"
    assert coverage.missing_surfaces == []


def test_absent_h5_candidate_is_not_a_missing_surface() -> None:
    _, coverage = _compile(
        _profile(["android_native"]),
        [_requirement("android_native"), _requirement("frontend_h5")],
    )

    by_surface = {unit.surface: unit for unit in coverage.units}
    assert by_surface["android_native"].coverage_status == "planned"
    assert by_surface["frontend_h5"].coverage_status == "not_required"
    assert coverage.missing_surfaces == []


def test_claim_routes_only_create_units_selected_for_present_surfaces() -> None:
    profile = _profile(["android_native"])
    source_ref = SourceRef(source_id="policy-1", source_section="section-1")
    claim = EvidenceClaim(
        claim_id="disclosure-entry",
        statement="A disclosure entry exists before the relevant action.",
        obligation_ids=["obl.disclosure"],
        source_refs=[source_ref],
        proof_route_policy="any_one",
        proof_routes=[
            EvidenceProofRoute(
                route_id="android-route",
                surface="android_native",
                claim_to_prove="Native disclosure entry exists.",
                    expected_evidence_strength="static_proof",
                    why_this_surface="The app has a native delivery path.",
                    acceptance_criteria=[
                        AcceptanceCriterion(
                            criterion_id="android-entry",
                            criterion_type="presence",
                            statement="A native disclosure entry exists.",
                            scope="Production Android UI code.",
                        )
                    ],
                    proof_limits=["Does not prove runtime display."],
            ),
            EvidenceProofRoute(
                route_id="h5-route",
                surface="frontend_h5",
                claim_to_prove="H5 disclosure entry exists.",
                    expected_evidence_strength="static_proof",
                    why_this_surface="Only applicable when H5 is configured.",
                    acceptance_criteria=[
                        AcceptanceCriterion(
                            criterion_id="h5-entry",
                            criterion_type="presence",
                            statement="An H5 disclosure entry exists.",
                            scope="Production H5 routes and components.",
                        )
                    ],
                    proof_limits=["Not selected when H5 is absent."],
            ),
        ],
    )
    control = Control(
        control_id="control.claim-route",
        module_id="loan_disclosure",
        title="Claim route fixture",
        severity="high",
        applicability_condition=ApplicabilityCondition.unknown(
            "the product profile determines delivery"
        ),
        candidate_surfaces=["android_native", "frontend_h5"],
        required_surfaces=["android_native", "frontend_h5"],
        minimum_evidence_strength={
            "android_native": "static_proof",
            "frontend_h5": "static_proof",
        },
        missing_evidence_policy="block",
        source_refs=[source_ref],
        reuse_invalidation_keys=["control_version"],
        obligation_ids=["obl.disclosure"],
        evidence_requirements={
            "android_native": EvidenceRequirement(
                minimum_strength="static_proof",
                rationale="Native route summary.",
                obligation_ids=["obl.disclosure"],
                source_refs=[source_ref],
            ),
            "frontend_h5": EvidenceRequirement(
                minimum_strength="static_proof",
                rationale="H5 route summary.",
                obligation_ids=["obl.disclosure"],
                source_refs=[source_ref],
            ),
        },
        evidence_claims=[claim],
    )
    controls = ControlSet(contract="control_set.v2", version="1.0", controls=[control])
    applicability = ApplicabilitySet(
        profile_version=profile.version,
        control_version=controls.version,
        decisions=[
            {
                "control_id": control.control_id,
                "decision": "applicable",
                "reason": "The product is in scope.",
                "source_refs": [source_ref],
                "selected_route_ids": ["android-route"],
                "surface_requirements": [
                    {
                        "surface": "android_native",
                        "decision": "required",
                        "reason": "Configured delivery surface.",
                    },
                    {
                        "surface": "frontend_h5",
                        "decision": "not_required",
                        "reason": "H5 is absent from the confirmed profile.",
                    },
                ],
            }
        ],
    )

    coverage = CoverageUnitBuilder().build(
        profile, controls, applicability, available_surfaces={"android_native"}
    )

    assert [unit.route_id for unit in coverage.units] == ["android-route"]
    assert all(unit.surface == "android_native" for unit in coverage.units)
    assert not any(unit.surface == "frontend_h5" for unit in coverage.units)


def test_applicability_cannot_cancel_unconditional_required_surface() -> None:
    profile = _profile(["android_native", "frontend_h5"])
    profile.confirmed_facts["evidence_surfaces"] = ApplicabilityProfileFact(
        value=["android_native", "frontend_h5"], source="human_confirmed"
    )
    requirement = SurfaceRequirementDecision(
        surface="frontend_h5",
        decision="not_required",
        reason="This obligation is not delivered through the web surface.",
        source_refs=[SOURCE_REF],
        profile_fact_refs=[
            ProfileFactRef(
                field_name="evidence_surfaces",
                expected_value='["android_native","frontend_h5"]',
            )
        ],
    ).model_dump(mode="json")
    _, coverage = _compile(profile, [_requirement("android_native"), requirement])

    assert coverage.missing_surfaces == []
    assert coverage.units[1].coverage_status == "planned"


def test_invalid_source_reference_is_conservative_unknown() -> None:
    control = _control()

    def response_factory(_: ModelRequest) -> ModelResponse:
        payload = {
            "decisions": [
                {
                    "control_id": control.control_id,
                    "decision": "not_applicable",
                    "reason": "Invented source exclusion.",
                    "source_refs": [{"source_id": "invented", "source_section": "x"}],
                    "profile_fact_refs": [],
                    "surface_requirements": [],
                    "unresolved_conditions": [],
                    "confidence": "high",
                }
            ]
        }
        return ModelResponse(content=json.dumps(payload))

    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    result = ApplicabilityEngine(StaticModelProvider(response_factory)).evaluate(
        _profile(["android_native"]), controls
    )
    assert result.decisions[0].decision == "unknown"
    assert result.unknown_control_ids == [control.control_id]


def test_model_profile_reference_cannot_override_profile_surface_set() -> None:
    bad_requirement = _not_required("frontend_h5")
    bad_requirement["profile_fact_refs"] = [
        {"field_name": "evidence_surfaces", "expected_value": "false"}
    ]
    _, coverage = _compile(
        _profile(["android_native"]), [_requirement("android_native"), bad_requirement]
    )
    assert coverage.units[1].coverage_status == "not_required"


@pytest.mark.parametrize("source", ["inferred", "unresolved"])
def test_untrusted_profile_source_still_uses_profile_surface_set(source: str) -> None:
    profile = _profile(["android_native"])
    profile.confirmed_facts["evidence_surfaces"] = ApplicabilityProfileFact(
        value=["android_native"], source=source  # type: ignore[arg-type]
    )
    requirement = _not_required("frontend_h5")
    _, coverage = _compile(profile, [_requirement("android_native"), requirement])
    assert coverage.units[1].coverage_status == "not_required"


def test_deterministic_profile_source_marks_absent_candidate_not_required() -> None:
    profile = _profile(["android_native"])
    profile.confirmed_facts["evidence_surfaces"] = ApplicabilityProfileFact(
        value=["android_native"], source="deterministic"
    )
    _, coverage = _compile(
        profile,
        [_requirement("android_native"), _not_required("frontend_h5")],
    )
    assert coverage.units[1].coverage_status == "not_required"


def test_control_defined_surface_condition_resolves_h5_from_app_profile() -> None:
    control = _control().model_copy(
        update={
            "evidence_requirements": {
                "android_native": EvidenceRequirement(
                    minimum_strength="static_proof", rationale="Native disclosure."
                ),
                "frontend_h5": EvidenceRequirement(
                    minimum_strength="static_proof",
                    rationale="H5 disclosure when the app has an H5 surface.",
                    condition=ApplicabilityCondition(
                        kind="atom",
                        fact="evidence_surfaces",
                        operator="includes",
                        value="frontend_h5",
                    ),
                ),
            }
        }
    )
    controls = ControlSet(contract="control_set.v2", version="1.0", controls=[control])
    profile = _profile(["android_native"])
    applicability = ApplicabilityEngine(_provider([])).evaluate(profile, controls)
    coverage = CoverageUnitBuilder().build(profile, controls, applicability)

    decision = applicability.decisions[0]
    assert decision.resolved_required_surfaces == ["android_native"]
    by_surface = {unit.surface: unit for unit in coverage.units}
    assert by_surface["android_native"].coverage_status == "planned"
    assert by_surface["frontend_h5"].coverage_status == "not_required"

    profile_with_h5 = _profile(["android_native", "frontend_h5"])
    applicability_with_h5 = ApplicabilityEngine(_provider([])).evaluate(
        profile_with_h5, controls
    )
    coverage_with_h5 = CoverageUnitBuilder().build(
        profile_with_h5, controls, applicability_with_h5
    )
    assert applicability_with_h5.decisions[0].resolved_required_surfaces == [
        "android_native",
        "frontend_h5",
    ]
    assert (
        {unit.surface: unit for unit in coverage_with_h5.units}["frontend_h5"].coverage_status
        == "planned"
    )


def test_semantic_payload_contains_absent_surface_and_policy_context() -> None:
    captured: dict[str, object] = {}
    control = _control().model_copy(update={"obligation_ids": ["obl.disclosure"]})
    controls = ControlSet(contract="control_set.v1", version="1.0", controls=[control])
    source = ComplianceSource(
        source_id="policy-1",
        path="policy.md",
        title="Disclosure policy",
        sha256="0" * 64,
        source_family="country_regulator",
        media_type="md",
        extraction_status="ok",
        sections=[
            SourceSection(
                section_id="section-1",
                title="Disclosure",
                text="A generic disclosure must be shown before the relevant action.",
                ordinal=1,
            )
        ],
    )
    obligation = Obligation(
        obligation_id="obl.disclosure",
        source_id="policy-1",
        source_section="section-1",
        statement="A generic disclosure must be shown before the relevant action.",
        concepts=["disclosure"],
        applicability_expression="business_type includes personal_loan",
        required_surfaces=["android_native"],
        source_refs=[SOURCE_REF],
    )

    def response_factory(request: ModelRequest) -> ModelResponse:
        captured.update(json.loads(request.messages[1]["content"]))
        return ModelResponse(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "control_id": control.control_id,
                            "decision": "applicable",
                            "reason": "The obligation applies.",
                            "source_refs": [SOURCE_REF.model_dump(mode="json")],
                            "profile_fact_refs": [],
                            "surface_requirements": [
                                _requirement("android_native"),
                                _not_required("frontend_h5"),
                            ],
                            "unresolved_conditions": [],
                            "confidence": "high",
                        }
                    ]
                }
            )
        )

    SemanticApplicabilityEvaluator(StaticModelProvider(response_factory)).evaluate(
        _profile(["android_native"]),
        controls,
        source_registry=SourceRegistry(version="1.0", sources=[source]),
        obligations=[obligation],
    )
    surface_facts = {item["surface"]: item for item in captured["delivery_surface_facts"]}
    assert surface_facts["android_native"]["present"] is True
    assert surface_facts["frontend_h5"]["present"] is False
    policy_context = captured["controls"][0]["obligations"][0]
    assert policy_context["statement"] == obligation.statement
    assert policy_context["section"]["text"] == source.sections[0].text
    assert captured["controls"][0]["candidate_surfaces"] == [
        "android_native",
        "frontend_h5",
    ]


def test_illegal_surface_is_rejected_by_structured_contract() -> None:
    def response_factory(_: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "control_id": "control.disclosure",
                            "decision": "applicable",
                            "reason": "Invalid surface fixture.",
                            "source_refs": [SOURCE_REF.model_dump(mode="json")],
                            "profile_fact_refs": [],
                            "surface_requirements": [
                                {
                                    "surface": "ios_native",
                                    "decision": "required",
                                    "reason": "invalid",
                                    "source_refs": [SOURCE_REF.model_dump(mode="json")],
                                    "profile_fact_refs": [],
                                }
                            ],
                            "unresolved_conditions": [],
                            "confidence": "high",
                        }
                    ]
                }
            )
        )

    evaluator = SemanticApplicabilityEvaluator(StaticModelProvider(response_factory))
    with pytest.raises(ValueError, match="valid decisions"):
        evaluator.evaluate(_profile(["android_native"]), ControlSet(
            contract="control_set.v1", version="1.0", controls=[_control()]
        ))
