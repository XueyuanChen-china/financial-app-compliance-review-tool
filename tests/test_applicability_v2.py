from __future__ import annotations

import json

from compliance_review.compilation.models import Obligation
from compliance_review.domain.models import (
    ApplicabilityCondition,
    ApplicabilityDecision,
    ApplicabilityDiscoveryPlan,
    ApplicabilityDiscoveryResultSet,
    ApplicabilityDiscoveryWorkItem,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    ApplicabilitySet,
    Control,
    ControlSet,
    EvidenceRequirement,
    Fact,
    SourceRef,
)
from compliance_review.review.applicability import SemanticApplicabilityEvaluator
from compliance_review.review.models import ModelResponse
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.models import AppFactSet, RepositoryInventory
from compliance_review.setup.planning import (
    ApplicabilityDiscoveryExecutor,
    ApplicabilityDiscoveryPlanner,
)

SOURCE = SourceRef(source_id="policy", source_section="section-1")


def _profile() -> ApplicabilityProfile:
    return ApplicabilityProfile(
        contract="applicability_profile.v1",
        version="1.0",
        app_name="Example",
        package_name="com.example",
        jurisdiction="Pakistan",
        business_type=["personal_loan"],
        self_lending="unknown",
        evidence_surfaces=["android_native", "backend_code"],
        review_scope="multi_surface_static_review",
        roots={"android_native": "/repo/android", "backend_code": "/repo/backend"},
        confirmed_facts={
            "evidence_surfaces": ApplicabilityProfileFact(
                value=["android_native", "backend_code"], source="human_confirmed"
            )
        },
    )


def _control(condition: ApplicabilityCondition) -> Control:
    return Control(
        control_id="control.ewa",
        module_id="loan",
        title="EWA applicability",
        severity="high",
        obligation_ids=["obl.ewa"],
        applicability_condition=condition,
        required_surfaces=["android_native", "backend_code"],
        minimum_evidence_strength={
            "android_native": "static_proof",
            "backend_code": "server_code",
        },
        missing_evidence_policy="block",
        source_refs=[SOURCE],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "android_native": EvidenceRequirement(
                minimum_strength="static_proof",
                rationale="Native behavior must be checked.",
                obligation_ids=["obl.ewa"],
                source_refs=[SOURCE],
            ),
            "backend_code": EvidenceRequirement(
                minimum_strength="server_code",
                rationale="Server behavior must be checked.",
                obligation_ids=["obl.ewa"],
                source_refs=[SOURCE],
            ),
        },
    )


def test_legacy_or_condition_is_unknown_and_not_flattened() -> None:
    control = Control(
        control_id="control.or",
        module_id="loan",
        title="OR condition",
        severity="medium",
        applicability_expression=(
            "business_type includes personal_loan or earned_wage_access == true"
        ),
        required_surfaces=["android_native"],
        minimum_evidence_strength={"android_native": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[SOURCE],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "android_native": EvidenceRequirement(
                minimum_strength="static_proof", rationale="Check native behavior."
            )
        },
    )
    assert control.applicability_condition.kind == "unknown"
    assert "applicability_expression" not in control.model_dump(mode="json")


def test_structured_any_of_is_preserved() -> None:
    condition = ApplicabilityCondition(
        kind="any_of",
        conditions=[
            ApplicabilityCondition(
                kind="atom", fact="business_type", operator="includes", value="personal_loan"
            ),
            ApplicabilityCondition(
                kind="atom", fact="earned_wage_access", operator="equals", value=True
            ),
        ],
    )
    assert condition.model_dump(mode="json")["kind"] == "any_of"
    assert len(condition.conditions) == 2


def test_semantic_applicability_payload_uses_structured_condition_not_legacy_dsl() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="earned_wage_access", operator="equals", value=True
        )
    )

    def response(request: object) -> ModelResponse:
        payload = json.loads(request.messages[1]["content"])  # type: ignore[attr-defined]
        obligation = payload["controls"][0]["obligations"][0]
        assert "applicability_expression" not in obligation
        assert obligation["applicability_condition"]["kind"] == "atom"
        return ModelResponse(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "control_id": control.control_id,
                            "decision": "unknown",
                            "reason": "The business fact is unresolved.",
                        }
                    ]
                }
            )
        )

    SemanticApplicabilityEvaluator(StaticModelProvider(response)).evaluate(
        _profile(),
        ControlSet(contract="control_set.v1", version="1.0", controls=[control]),
        obligations=[
            Obligation(
                obligation_id="obl.ewa",
                source_id="policy",
                source_section="section-1",
                statement="The policy applies to earned wage access loans.",
                concepts=["earned_wage_access"],
                applicability_condition=control.applicability_condition,
                required_surfaces=["android_native", "backend_code"],
                source_refs=[SOURCE],
            )
        ],
    )


def test_unknown_applicability_creates_deduplicated_discovery_work_item() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="earned_wage_access", operator="equals", value=True
        )
    )
    applicability = ApplicabilitySet(
        profile_version="1.0",
        control_version="1.0",
        decisions=[
            ApplicabilityDecision(
                control_id=control.control_id,
                decision="unknown",
                reason="EWA fact is unresolved.",
                source_refs=[SOURCE],
                unresolved_conditions=["earned_wage_access"],
            )
        ],
        unknown_control_ids=[control.control_id],
    )
    inventory = RepositoryInventory(
        repo_id="android",
        path="/repo/android",
        detected_surface="android_native",
        detected_surfaces=["android_native"],
        surface_status="confirmed",
    )
    plan = ApplicabilityDiscoveryPlanner().plan(
        _profile(),
        controls=ControlSet(contract="control_set.v1", version="1.0", controls=[control]),
        applicability=applicability,
        inventories=[inventory],
    )
    assert len(plan.work_items) == 1
    assert plan.work_items[0].unresolved_fact_keys == ["earned_wage_access"]
    assert plan.work_items[0].dependent_control_ids == [control.control_id]


def test_discovery_normalizes_natural_language_unknown_to_canonical_fact_key() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="unknown",
            reason=(
                "Applies to apps that provide Earned Wage Access loans, including "
                "facilitators connecting consumers with third-party lenders."
            ),
        )
    )
    applicability = ApplicabilitySet(
        profile_version="1.0",
        control_version="1.0",
        decisions=[
            ApplicabilityDecision(
                control_id=control.control_id,
                decision="unknown",
                reason="The supplied profile does not confirm the product capability.",
                source_refs=[SOURCE],
                unresolved_conditions=[
                    "Whether the app provides or facilitates Earned Wage Access loans "
                    "is unconfirmed."
                ],
            )
        ],
        unknown_control_ids=[control.control_id],
    )
    inventory = RepositoryInventory(
        repo_id="android",
        path="/repo/android",
        detected_surface="android_native",
        detected_surfaces=["android_native"],
        surface_status="confirmed",
    )

    plan = ApplicabilityDiscoveryPlanner().plan(
        _profile(),
        controls=ControlSet(contract="control_set.v1", version="1.0", controls=[control]),
        applicability=applicability,
        inventories=[inventory],
    )

    assert len(plan.work_items) == 1
    assert "earned_wage_access" in plan.work_items[0].unresolved_fact_keys
    assert control.control_id not in plan.terminal_gaps


def test_discovery_executor_resolves_technical_fact_but_keeps_business_fact_candidate() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="any_of",
            conditions=[
                ApplicabilityCondition(
                    kind="atom",
                    fact="loan_application_flow_present",
                    operator="equals",
                    value=True,
                ),
                ApplicabilityCondition(
                    kind="atom", fact="earned_wage_access", operator="equals", value=True
                ),
            ],
        )
    )
    applicability = ApplicabilitySet(
        profile_version="1.0",
        control_version="1.0",
        decisions=[
            ApplicabilityDecision(
                control_id=control.control_id,
                decision="unknown",
                reason="Facts need bounded discovery.",
                source_refs=[SOURCE],
                unresolved_conditions=["loan_application_flow_present", "earned_wage_access"],
            )
        ],
        unknown_control_ids=[control.control_id],
    )
    inventory = RepositoryInventory(
        repo_id="backend",
        path="/repo/backend",
        detected_surface="backend_code",
        detected_surfaces=["backend_code"],
        surface_status="confirmed",
    )
    plan = ApplicabilityDiscoveryPlanner().plan(
        _profile(),
        ControlSet(contract="control_set.v1", version="1.0", controls=[control]),
        applicability,
        [inventory],
    )
    facts = AppFactSet(
        inventory_ids=["backend"],
        facts=[
            Fact(
                fact_id="fact.backend.endpoint.loan-apply",
                repo_id="backend",
                source_surface="backend_code",
                fact_type="declared_api_endpoint",
                observed_value={"route": "/loans/apply", "method": "POST"},
                source_refs=[SourceRef(path="/repo/backend/routes.py")],
                parser_status="ok",
                coverage_status="complete",
                evidence_strength="server_code",
            )
        ],
    )
    result = ApplicabilityDiscoveryExecutor().execute(plan, facts)

    assert result.barrier_complete is True
    assert result.results[0].terminal_status == "manual_required"
    by_key = {fact.fact_key: fact for fact in result.results[0].facts}
    assert by_key["loan_application_flow_present"].status == "verified"
    assert by_key["earned_wage_access"].status == "unresolved"
    assert not hasattr(result.results[0], "decision")


def test_discovery_does_not_treat_every_android_permission_as_sensitive() -> None:
    plan = ApplicabilityDiscoveryPlan(
        preparation_version="applicability-prep-v2",
        work_items=[
            ApplicabilityDiscoveryWorkItem(
                discovery_id="adw.sensitive-permissions",
                unresolved_fact_keys=["sensitive_permission_use_present"],
                dependent_control_ids=["control.sensitive-permissions"],
                allowed_surfaces=["android_native"],
                allowed_roots={"android_native": ["/repo/android"]},
                preparation_version="applicability-prep-v2",
            )
        ],
    )
    facts = AppFactSet(
        inventory_ids=["android"],
        facts=[
            Fact(
                fact_id="fact.android.permission.internet",
                repo_id="android",
                source_surface="android_native",
                fact_type="android_manifest_permission",
                observed_value="android.permission.INTERNET",
                source_refs=[SourceRef(path="/repo/android/AndroidManifest.xml")],
                parser_status="ok",
                coverage_status="complete",
                evidence_strength="static_proof",
            )
        ],
    )

    result = ApplicabilityDiscoveryExecutor().execute(plan, facts)

    assert result.results[0].facts[0].status == "unresolved"
    assert result.results[0].terminal_status == "failed_exhausted"


def test_verified_discovery_fact_is_applied_for_one_recheck_only() -> None:
    profile = _profile()
    result = ApplicabilityDiscoveryResultSet(
        preparation_version="applicability-prep-v2",
        barrier_complete=True,
        results=[],
    )
    derived = ApplicabilityDiscoveryExecutor.apply_verified_facts(profile, result)
    assert derived.confirmed_facts == profile.confirmed_facts
