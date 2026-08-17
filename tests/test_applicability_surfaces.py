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
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    Control,
    ControlSet,
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


def test_android_only_disclosure_does_not_create_h5_gap() -> None:
    applicability, coverage = _compile(
        _profile(["android_native"]),
        [_requirement("android_native"), _not_required("frontend_h5")],
    )

    by_surface = {unit.surface: unit for unit in coverage.units}
    assert applicability.decisions[0].decision == "applicable"
    assert by_surface["android_native"].coverage_status == "planned"
    assert by_surface["frontend_h5"].coverage_status == "not_applicable"
    assert coverage.missing_surfaces == []


def test_mobile_and_web_control_blocks_when_h5_surface_is_missing() -> None:
    _, coverage = _compile(
        _profile(["android_native"]),
        [_requirement("android_native"), _requirement("frontend_h5")],
    )

    by_surface = {unit.surface: unit for unit in coverage.units}
    assert by_surface["android_native"].coverage_status == "planned"
    assert by_surface["frontend_h5"].coverage_status == "missing_surface"
    assert coverage.missing_surfaces == ["frontend_h5"]


def test_offered_web_surface_can_be_explicitly_not_required_for_this_control() -> None:
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
    assert coverage.units[1].coverage_status == "not_applicable"


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


def test_invalid_profile_reference_is_conservative_unknown_surface() -> None:
    bad_requirement = _not_required("frontend_h5")
    bad_requirement["profile_fact_refs"] = [
        {"field_name": "evidence_surfaces", "expected_value": "false"}
    ]
    _, coverage = _compile(
        _profile(["android_native"]), [_requirement("android_native"), bad_requirement]
    )
    assert coverage.units[1].coverage_status == "unknown_applicability"


@pytest.mark.parametrize("source", ["inferred", "unresolved"])
def test_untrusted_profile_source_is_conservative_unknown_surface(source: str) -> None:
    profile = _profile(["android_native"])
    profile.confirmed_facts["evidence_surfaces"] = ApplicabilityProfileFact(
        value=["android_native"], source=source  # type: ignore[arg-type]
    )
    requirement = _not_required("frontend_h5")
    _, coverage = _compile(profile, [_requirement("android_native"), requirement])
    assert coverage.units[1].coverage_status == "unknown_applicability"


def test_deterministic_profile_source_is_trusted_for_surface_requirement() -> None:
    profile = _profile(["android_native"])
    profile.confirmed_facts["evidence_surfaces"] = ApplicabilityProfileFact(
        value=["android_native"], source="deterministic"
    )
    _, coverage = _compile(
        profile,
        [_requirement("android_native"), _not_required("frontend_h5")],
    )
    assert coverage.units[1].coverage_status == "not_applicable"


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
