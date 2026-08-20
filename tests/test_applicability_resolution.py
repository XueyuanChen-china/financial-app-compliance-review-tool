from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from compliance_review.domain.models import (
    ApplicabilityCondition,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    Control,
    ControlSet,
    EvidenceRequirement,
    Fact,
    SourceRef,
)
from compliance_review.review.applicability import ApplicabilityResolutionLoop
from compliance_review.review.models import ModelRequest, ModelResponse
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.models import AppFactSet, RepositoryInventory

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "day2" / "android"
SOURCE_REF = SourceRef(url="https://example.test/policy")


def _profile() -> ApplicabilityProfile:
    return ApplicabilityProfile(
        contract="applicability_profile.v2",
        version="2.0",
        app_name="unknown",
        package_name="unknown",
        jurisdiction="unknown",
        business_type=["unknown"],
        self_lending="unknown",
        evidence_surfaces=["android_native"],
        review_scope="multi_surface_static_review",
        roots={"android_native": FIXTURE_ROOT.as_posix()},
    )


def _control(condition: ApplicabilityCondition) -> Control:
    return Control(
        control_id="control.loan",
        module_id="loan",
        title="Loan applicability",
        severity="high",
        applicability_condition=condition,
        required_surfaces=["android_native"],
        minimum_evidence_strength={"android_native": "static_proof"},
        missing_evidence_policy="block",
        source_refs=[SOURCE_REF],
        reuse_invalidation_keys=["control_version"],
        evidence_requirements={
            "android_native": EvidenceRequirement(
                minimum_strength="static_proof", rationale="Inspect the app."
            )
        },
    )


def _inventory() -> RepositoryInventory:
    return RepositoryInventory(
        repo_id="android",
        path=FIXTURE_ROOT.as_posix(),
        detected_surface="android_native",
        detected_surfaces=["android_native"],
        surface_status="confirmed",
    )


def test_unknown_produces_one_deduplicated_human_question() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )

    def response(_: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control.control_id,
                    "decision": "unknown",
                    "reason": "Whether the business is self-lending is unresolved.",
                    "unresolved_conditions": ["self_lending is unresolved"],
                    "confidence": "low",
                }
            )
        )

    _, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response)
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert resolution.status == "awaiting_human"
    assert resolution.pending_questions[0].fact_key == "self_lending"
    assert applicability.unknown_control_ids == [control.control_id]


def test_human_answer_is_applied_before_the_final_applicability_decision() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )

    def response(request: ModelRequest) -> ModelResponse:
        payload = json.loads(request.messages[1]["content"])
        value = payload["confirmed_profile_facts"].get("self_lending", {}).get("value")
        return ModelResponse(
            content=json.dumps(
                    {
                        "control_id": control.control_id,
                        "decision": "applicable" if value is True else "unknown",
                        "reason": "Confirmed business fact." if value is True else "Missing fact.",
                        "profile_fact_refs": (
                            [{"field_name": "self_lending", "expected_value": "true"}]
                            if value is True
                            else []
                        ),
                        "unresolved_conditions": [] if value is True else ["self_lending"],
                    "confidence": "high" if value is True else "low",
                }
            )
        )

    profile, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response)
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
        human_answers={"self_lending": True},
    )

    assert profile.self_lending is True
    assert applicability.decisions[0].decision == "applicable"
    assert resolution.status == "complete"


def test_loop_can_use_surface_scoped_read_only_tool_before_deciding() -> None:
    control = _control(ApplicabilityCondition.unknown("inspect repository"))
    calls: list[str] = []

    def response(request: ModelRequest) -> ModelResponse:
        if not any(message.get("role") == "tool" for message in request.messages):
            return ModelResponse(
                tool_calls=[
                    {
                        "call_id": "call-1",
                        "name": "search_code",
                        "arguments": {"surface": "android_native", "query": "<manifest"},
                    }
                ]
            )
        calls.append("tool-returned")
        return ModelResponse(
            content=json.dumps(
                    {
                        "control_id": control.control_id,
                        "decision": "applicable",
                        "reason": "Repository inspection completed.",
                        "technical_fact_refs": ["fact.android.manifest.1"],
                        "unresolved_conditions": [],
                    "confidence": "medium",
                }
            )
        )

    _, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response), max_tool_rounds=2
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(
            inventory_ids=["android"],
            facts=[
                Fact(
                    fact_id="fact.android.manifest.1",
                    repo_id="android",
                    source_surface="android_native",
                    fact_type="manifest_file_present",
                    observed_value=True,
                    source_refs=[SourceRef(path="AndroidManifest.xml")],
                    parser_status="ok",
                    coverage_status="complete",
                    evidence_strength="static_proof",
                )
            ],
        ),
    )

    assert calls == ["tool-returned"]
    assert resolution.tool_calls == 1
    assert applicability.decisions[0].decision == "applicable"


def test_unknown_technical_fact_reference_is_rejected_and_becomes_unknown() -> None:
    control = _control(ApplicabilityCondition.unknown("missing fact"))

    def response(_: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control.control_id,
                    "decision": "applicable",
                    "reason": "Unverified fact.",
                    "technical_fact_refs": ["fact.does-not-exist"],
                    "unresolved_conditions": [],
                    "confidence": "high",
                }
            )
        )

    _, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response), max_validation_retries=0
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert applicability.decisions[0].decision == "unknown"
    assert resolution.validation_issues
    assert "allowed IDs are []" in resolution.validation_issues[0].message


def test_invalid_source_reference_is_returned_to_model_for_retry() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )
    calls = 0

    def response(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        source_refs = (
            [{"source_id": "wrong-source", "source_section": "section-1"}]
            if calls == 1
            else []
        )
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control.control_id,
                    "decision": "applicable",
                    "reason": "Confirmed business fact.",
                    "source_refs": source_refs,
                    "profile_fact_refs": [
                        {"field_name": "self_lending", "expected_value": "true"}
                    ],
                    "unresolved_conditions": [],
                    "confidence": "high",
                }
            )
        )

    _, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response), max_validation_retries=1
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
        human_answers={"self_lending": True},
    )

    assert calls == 2
    assert applicability.decisions[0].decision == "applicable"
    assert resolution.status == "complete"


def test_invalid_profile_fact_reference_is_returned_with_canonical_value_for_retry() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )
    calls = 0

    def response(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        payload = json.loads(request.messages[1]["content"])
        allowed = payload["allowed_profile_fact_refs"]
        expected_value = (
            "not-json" if calls == 1 else next(
                item["expected_value"]
                for item in allowed
                if item["field_name"] == "self_lending"
            )
        )
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control.control_id,
                    "decision": "applicable",
                    "reason": "Confirmed business fact.",
                    "profile_fact_refs": [
                        {"field_name": "self_lending", "expected_value": expected_value}
                    ],
                    "unresolved_conditions": [],
                    "confidence": "high",
                }
            )
        )

    _, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response), max_validation_retries=1
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
        human_answers={"self_lending": True},
    )

    assert calls == 2
    assert applicability.decisions[0].decision == "applicable"
    assert resolution.status == "complete"


def test_unknown_condition_accepts_valid_confirmed_profile_provenance() -> None:
    control = _control(ApplicabilityCondition.unknown("financial feature presence"))
    profile = _profile().model_copy(
        update={
            "business_type": ["banking"],
            "confirmed_facts": {
                "business_type": ApplicabilityProfileFact(
                    value=["banking"], source="human_confirmed"
                )
            },
        }
    )

    def response(_: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control.control_id,
                    "decision": "applicable",
                    "reason": "The confirmed business type indicates financial features.",
                    "profile_fact_refs": [
                        {
                            "field_name": "business_type",
                            "expected_value": '["banking"]',
                        }
                    ],
                    "unresolved_conditions": [],
                    "confidence": "medium",
                }
            )
        )

    _, applicability, resolution = ApplicabilityResolutionLoop(
        StaticModelProvider(response)
    ).resolve(
        profile,
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert applicability.decisions[0].decision == "applicable"
    assert resolution.status == "complete"


def test_applicability_controls_run_with_a_maximum_of_three_workers() -> None:
    controls = [
        _control(ApplicabilityCondition.unknown(f"fact-{index}")).model_copy(
            update={"control_id": f"control.loan.{index}"}
        )
        for index in range(6)
    ]
    lock = threading.Lock()
    active = 0
    peak_active = 0

    class BoundedProvider:
        def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                time.sleep(0.02)
                return ModelResponse(
                    content=json.dumps(
                        {
                            "control_id": request.work_item.control_id,
                            "decision": "unknown",
                            "reason": "The test fact is unresolved.",
                            "unresolved_conditions": ["test fact is unresolved"],
                            "confidence": "low",
                        }
                    )
                )
            finally:
                with lock:
                    active -= 1

    _, applicability, resolution = ApplicabilityResolutionLoop(
        BoundedProvider(), max_concurrency=3
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=controls),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert peak_active == 3
    assert len(applicability.decisions) == 6
    assert resolution.attempts == 6


def test_applicability_rejects_a_concurrency_limit_above_three() -> None:
    with pytest.raises(ValueError, match="between 1 and 3"):
        ApplicabilityResolutionLoop(None, max_concurrency=4)


def test_missing_provider_never_uses_legacy_applicability_hint() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )

    _, applicability, resolution = ApplicabilityResolutionLoop(None).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert applicability.decisions[0].decision == "unknown"
    assert resolution.status == "awaiting_human"


def test_unknown_without_pending_question_is_partial_not_complete() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )
    profile = _profile().model_copy(
        update={
            "self_lending": True,
            "confirmed_facts": {
                "self_lending": ApplicabilityProfileFact(
                    value=True, source="human_confirmed"
                )
            },
        }
    )

    _, applicability, resolution = ApplicabilityResolutionLoop(None).resolve(
        profile,
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert applicability.decisions[0].decision == "unknown"
    assert resolution.pending_questions == []
    assert resolution.status == "partial"


def test_provider_failure_is_downgraded_to_unknown() -> None:
    control = _control(ApplicabilityCondition.unknown("provider failure fixture"))

    class FailingProvider:
        def complete(self, _: ModelRequest) -> ModelResponse:
            raise RuntimeError("temporary provider outage")

    _, applicability, resolution = ApplicabilityResolutionLoop(
        FailingProvider(), max_validation_retries=0
    ).resolve(
        _profile(),
        ControlSet(contract="control_set.v2", version="1", controls=[control]),
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
    )

    assert applicability.decisions[0].decision == "unknown"
    assert resolution.validation_issues


def test_human_answer_keys_and_types_are_validated() -> None:
    control = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )
    loop = ApplicabilityResolutionLoop(None)

    with pytest.raises(ValueError, match="self_lending"):
        loop.resolve(
            _profile(),
            ControlSet(contract="control_set.v2", version="1", controls=[control]),
            [_inventory()],
            AppFactSet(inventory_ids=["android"]),
            human_answers={"self_lending": "yes"},
        )

    with pytest.raises(ValueError, match="unsupported"):
        loop.resolve(
            _profile(),
            ControlSet(contract="control_set.v2", version="1", controls=[control]),
            [_inventory()],
            AppFactSet(inventory_ids=["android"]),
            human_answers={"unrelated_fact": True},
        )


def test_resolution_checkpoint_is_emitted_per_control_and_resumes() -> None:
    first = _control(
        ApplicabilityCondition(
            kind="atom", fact="self_lending", operator="equals", value=True
        )
    )
    second = first.model_copy(update={"control_id": "control.loan.second"})
    controls = ControlSet(contract="control_set.v2", version="1", controls=[first, second])
    provider_calls: list[str] = []

    def response(request: ModelRequest) -> ModelResponse:
        payload = json.loads(request.messages[1]["content"])
        control_id = payload["control"]["control_id"]
        provider_calls.append(control_id)
        return ModelResponse(
            content=json.dumps(
                {
                    "control_id": control_id,
                    "decision": "applicable",
                    "reason": "Confirmed business fact.",
                    "profile_fact_refs": [
                        {"field_name": "self_lending", "expected_value": "true"}
                    ],
                    "unresolved_conditions": [],
                    "confidence": "high",
                }
            )
        )

    checkpoints: list[tuple[list[str], int, int]] = []
    loop = ApplicabilityResolutionLoop(StaticModelProvider(response), max_concurrency=1)
    _, _, first_resolution = loop.resolve(
        _profile(),
        controls,
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
        human_answers={"self_lending": True},
        checkpoint_callback=lambda decisions, attempts, tool_calls: checkpoints.append(
            ([item.control_id for item in decisions], attempts, tool_calls)
        ),
    )

    assert first_resolution.status == "complete"
    assert [item[0] for item in checkpoints] == [
        ["control.loan"],
        ["control.loan", "control.loan.second"],
    ]
    assert provider_calls == ["control.loan", "control.loan.second"]

    provider_calls.clear()
    _, _, resumed_resolution = loop.resolve(
        _profile(),
        controls,
        [_inventory()],
        AppFactSet(inventory_ids=["android"]),
        human_answers={"self_lending": True},
        initial_decisions=first_resolution.decisions[:1],
        initial_attempts=first_resolution.attempts,
        initial_tool_calls=first_resolution.tool_calls,
    )

    assert resumed_resolution.status == "complete"
    assert provider_calls == ["control.loan.second"]
