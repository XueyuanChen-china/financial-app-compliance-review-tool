from __future__ import annotations

import json
import re

from compliance_review.domain.models import (
    ApplicabilityDecision,
    ApplicabilityProfile,
    ContractModel,
    Control,
    ControlSet,
    ProfileFactRef,
    SourceRef,
    WorkItem,
)
from compliance_review.review.models import ModelRequest
from compliance_review.review.provider import ModelProvider

_EQUALS_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<value>.+)$")
_INCLUDES_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+includes\s+(?P<value>.+)$")
_IN_RE = re.compile(r"^(?P<value>[A-Za-z0-9_.-]+)\s+in\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)$")


class SemanticApplicabilityResponse(ContractModel):
    """Top-level model contract retains Pydantic $defs for nested references."""

    decisions: list[ApplicabilityDecision]


class ApplicabilityValidator:
    """Verify semantic claims without trying to reinterpret policy prose."""

    def validate(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        decisions: list[ApplicabilityDecision],
    ) -> list[ApplicabilityDecision]:
        by_id = {decision.control_id: decision for decision in decisions}
        expected_ids = {control.control_id for control in controls.controls}
        if set(by_id) != expected_ids or len(by_id) != len(decisions):
            raise ValueError("applicability decisions must cover every Control exactly once")
        normalized: list[ApplicabilityDecision] = []
        for control in controls.controls:
            decision = by_id[control.control_id]
            source_refs_valid = _source_refs_valid(decision.source_refs, control.source_refs)
            profile_refs_valid = _profile_refs_valid(decision.profile_fact_refs, profile)
            if decision.decision == "not_applicable" and (
                not decision.source_refs
                or not decision.profile_fact_refs
                or not source_refs_valid
                or not profile_refs_valid
            ):
                normalized.append(
                    decision.model_copy(
                        update={
                            "decision": "unknown",
                            "reason": (
                                "not_applicable claim could not be verified against confirmed "
                                "profile facts and linked policy provenance; "
                                "retained conservatively"
                            ),
                            "unresolved_conditions": sorted(
                                set([*decision.unresolved_conditions, "unverified_not_applicable"])
                            ),
                            "confidence": "low",
                        }
                    )
                )
                continue
            if not source_refs_valid or not profile_refs_valid:
                normalized.append(
                    decision.model_copy(
                        update={
                            "decision": "unknown",
                            "reason": (
                                "applicability references do not match the linked policy source "
                                "or confirmed AppProfile facts"
                            ),
                            "unresolved_conditions": sorted(
                                set([*decision.unresolved_conditions, "unverified_references"])
                            ),
                            "confidence": "low",
                        }
                    )
                )
                continue
            normalized.append(decision)
        return normalized


class SemanticApplicabilityEvaluator:
    """One bounded structured call for applicability, not an Agent loop."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def evaluate(
        self, profile: ApplicabilityProfile, controls: ControlSet
    ) -> list[ApplicabilityDecision]:
        work_item = WorkItem(
            work_item_id="applicability.semantic",
            module_id="applicability",
            surface="other_external",
            control_ids=[control.control_id for control in controls.controls],
            allowed_roots=["."],
        )
        payload = {
            "confirmed_profile_facts": {
                name: fact.model_dump(mode="json") for name, fact in profile.confirmed_facts.items()
            },
            "controls": [
                {
                    "control_id": control.control_id,
                    "title": control.title,
                    "linked_policy_sources": [
                        reference.model_dump(mode="json") for reference in control.source_refs
                    ],
                    "legacy_applicability_hint": control.applicability_expression,
                }
                for control in controls.controls
            ],
        }
        request = ModelRequest(
            work_item=work_item,
            attempt_id="applicability.semantic.v1",
            agent_id="applicability-evaluator",
            request_kind="applicability",
            token_budget=8_000,
            tools=[],
            response_schema=SemanticApplicabilityResponse.model_json_schema(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Decide applicability for every supplied control using policy context and "
                        "only supplied confirmed AppProfile facts. unknown is required when a "
                        "condition cannot be confirmed. not_applicable is allowed only when "
                        "source_refs and profile_fact_refs cite the specific supplied facts that "
                        "prove exclusion. profile_fact_refs.expected_value must be the canonical "
                        "JSON string for the supplied profile value. Do not invent sources or "
                        "profile values."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        response = self.provider.complete(request)
        if response.tool_calls or not response.content:
            raise ValueError(
                "semantic applicability response must be structured JSON without tools"
            )
        try:
            parsed = SemanticApplicabilityResponse.model_validate_json(response.content)
        except ValueError as exc:
            raise ValueError("semantic applicability response is missing valid decisions") from exc
        return parsed.decisions


def legacy_applicability_decision(
    control: Control, profile: ApplicabilityProfile
) -> ApplicabilityDecision:
    """Conservative compatibility fallback when no semantic provider is configured."""
    result = control_applicability(control, profile)
    refs = _legacy_profile_refs(control.applicability_expression, profile)
    if result is True:
        return ApplicabilityDecision(
            control_id=control.control_id,
            decision="applicable",
            reason="legacy applicability hint matches confirmed AppProfile facts",
            source_refs=control.source_refs,
            profile_fact_refs=refs,
            confidence="medium",
        )
    return ApplicabilityDecision(
        control_id=control.control_id,
        decision="unknown",
        reason="semantic applicability provider is unavailable or the legacy hint is unresolved",
        source_refs=control.source_refs,
        profile_fact_refs=refs,
        unresolved_conditions=[control.applicability_expression],
        confidence="low",
    )


def control_applicability(control: Control, profile: ApplicabilityProfile) -> bool | None:
    """Evaluate the legacy compatibility hint only; it is not authoritative routing."""
    expression = control.applicability_expression.strip()
    if not expression or expression == "unknown":
        return None
    clauses = re.split(r"\s+(?:and|&&)\s+", expression, flags=re.IGNORECASE)
    results = [_evaluate_clause(clause.strip(), profile) for clause in clauses]
    if any(result is None for result in results):
        return None
    return all(result is True for result in results)


def _source_refs_valid(provided: list[SourceRef], allowed: list[SourceRef]) -> bool:
    allowed_refs = {_ref_key(reference) for reference in allowed}
    return all(_ref_key(reference) in allowed_refs for reference in provided)


def _profile_refs_valid(provided: list[ProfileFactRef], profile: ApplicabilityProfile) -> bool:
    for reference in provided:
        fact = profile.confirmed_facts.get(reference.field_name)
        if (
            fact is None
            or fact.source != "human_confirmed"
            or _canonical_json(fact.value) != reference.expected_value
        ):
            return False
    return True


def _ref_key(reference: SourceRef) -> str:
    return json.dumps(reference.model_dump(mode="json", exclude_none=True), sort_keys=True)


def _legacy_profile_refs(expression: str, profile: ApplicabilityProfile) -> list[ProfileFactRef]:
    names: set[str] = set()
    for clause in re.split(r"\s+(?:and|&&)\s+", expression, flags=re.IGNORECASE):
        match = _INCLUDES_RE.match(clause) or _EQUALS_RE.match(clause) or _IN_RE.match(clause)
        if match:
            names.add(match.group("field"))
    refs: list[ProfileFactRef] = []
    for name in sorted(names):
        fact = profile.confirmed_facts.get(name)
        if fact is not None:
            refs.append(ProfileFactRef(field_name=name, expected_value=_canonical_json(fact.value)))
    return refs


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evaluate_clause(clause: str, profile: ApplicabilityProfile) -> bool | None:
    match = _INCLUDES_RE.match(clause) or _EQUALS_RE.match(clause) or _IN_RE.match(clause)
    if not match:
        return None
    field = match.group("field")
    raw_value = match.group("value").strip().strip("\"'")
    if field == "business_type":
        return raw_value in profile.business_type
    if field == "evidence_surfaces":
        return raw_value in profile.evidence_surfaces
    if field == "self_lending":
        if profile.self_lending == "unknown":
            return None
        return profile.self_lending is (raw_value.lower() == "true")
    if field == "jurisdiction":
        return profile.jurisdiction == raw_value
    return None
