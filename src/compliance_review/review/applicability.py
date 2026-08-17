from __future__ import annotations

import json
import re

from compliance_review.compilation.models import Obligation, SourceRegistry
from compliance_review.domain.models import (
    ApplicabilityDecision,
    ApplicabilityProfile,
    ContractModel,
    Control,
    ControlSet,
    ProfileFactRef,
    SourceRef,
    Surface,
    SurfaceRequirementDecision,
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
        obligations: list[Obligation] | None = None,
        source_registry: SourceRegistry | None = None,
    ) -> list[ApplicabilityDecision]:
        by_id = {decision.control_id: decision for decision in decisions}
        expected_ids = {control.control_id for control in controls.controls}
        if set(by_id) != expected_ids or len(by_id) != len(decisions):
            raise ValueError("applicability decisions must cover every Control exactly once")
        normalized: list[ApplicabilityDecision] = []
        obligations_by_id = {
            obligation.obligation_id: obligation for obligation in obligations or []
        }
        for control in controls.controls:
            decision = by_id[control.control_id]
            allowed_source_refs = _allowed_source_refs(control, obligations_by_id)
            source_refs_valid = _source_refs_valid(
                decision.source_refs, allowed_source_refs, source_registry
            )
            profile_refs_valid = _profile_refs_valid(decision.profile_fact_refs, profile)
            surface_requirements = _validated_surface_requirements(
                control,
                decision.surface_requirements,
                profile,
                allowed_source_refs,
                source_registry,
            )
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
                            "surface_requirements": surface_requirements,
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
                            "surface_requirements": surface_requirements,
                        }
                    )
                )
                continue
            normalized.append(
                decision.model_copy(update={"surface_requirements": surface_requirements})
            )
        return normalized


class SemanticApplicabilityEvaluator:
    """One bounded structured call for applicability, not an Agent loop."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def evaluate(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        source_registry: SourceRegistry | None = None,
        obligations: list[Obligation] | None = None,
    ) -> list[ApplicabilityDecision]:
        work_item = WorkItem(
            work_item_id="applicability.semantic",
            module_id="applicability",
            surface="other_external",
            control_ids=[control.control_id for control in controls.controls],
            allowed_roots=["."],
        )
        payload: dict[str, object] = {
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
                    "obligations": _obligation_context(control, obligations or [], source_registry),
                    "candidate_surfaces": list(control.required_surfaces),
                    "legacy_applicability_hint": control.applicability_expression,
                }
                for control in controls.controls
            ],
        }
        candidate_surfaces = {
            surface
            for control in controls.controls
            for surface in control.required_surfaces
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
                        "profile values. For every candidate surface, return exactly one "
                        "surface_requirements item. A surface is required only when the linked "
                        "obligation semantics require that delivery surface; do not infer this "
                        "from whether the repository exists. Return not_required only with a "
                        "reason, linked source_refs, and confirmed profile_fact_refs."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        _with_delivery_surface_facts(
                            payload, profile, candidate_surfaces
                        ),
                        ensure_ascii=False,
                    ),
                },
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
            surface_requirements=_legacy_surface_requirements(control),
            confidence="medium",
        )
    return ApplicabilityDecision(
        control_id=control.control_id,
        decision="unknown",
        reason="semantic applicability provider is unavailable or the legacy hint is unresolved",
        source_refs=control.source_refs,
        profile_fact_refs=refs,
        unresolved_conditions=[control.applicability_expression],
        surface_requirements=_legacy_surface_requirements(control),
        confidence="low",
    )


def _validated_surface_requirements(
    control: Control,
    provided: list[SurfaceRequirementDecision],
    profile: ApplicabilityProfile,
    allowed_source_refs: list[SourceRef],
    source_registry: SourceRegistry | None,
) -> list[SurfaceRequirementDecision]:
    candidate_surfaces = set(control.required_surfaces)
    by_surface = {item.surface: item for item in provided}
    invalid_surfaces = set(by_surface) - candidate_surfaces
    normalized: list[SurfaceRequirementDecision] = []
    for surface in control.required_surfaces:
        item = by_surface.get(surface)
        if item is None or invalid_surfaces:
            normalized.append(
                _unknown_surface_requirement(
                    surface,
                    "surface requirement is missing or includes an invalid candidate surface",
                )
            )
            continue
        source_refs_valid = _source_refs_valid(
            item.source_refs, allowed_source_refs, source_registry
        )
        profile_refs_valid = _profile_refs_valid(
            item.profile_fact_refs,
            profile,
            trusted_sources={"human_confirmed", "deterministic"},
        )
        exclusion_refs_present = bool(item.source_refs and item.profile_fact_refs)
        if not item.source_refs or not source_refs_valid or not profile_refs_valid or (
            item.decision == "not_required" and not exclusion_refs_present
        ):
            normalized.append(
                _unknown_surface_requirement(
                    surface,
                    "surface requirement references could not be verified conservatively",
                )
            )
            continue
        normalized.append(item)
    return normalized


def _unknown_surface_requirement(surface: Surface, reason: str) -> SurfaceRequirementDecision:
    return SurfaceRequirementDecision(
        surface=surface,
        decision="unknown",
        reason=reason,
    )


def _legacy_surface_requirements(control: Control) -> list[SurfaceRequirementDecision]:
    return [
        SurfaceRequirementDecision(
            surface=surface,
            decision="required",
            reason="legacy control required_surfaces compatibility fallback",
            source_refs=control.source_refs,
        )
        for surface in control.required_surfaces
    ]


def _obligation_context(
    control: Control,
    obligations: list[Obligation],
    source_registry: SourceRegistry | None,
) -> list[dict[str, object]]:
    obligations_by_id = {obligation.obligation_id: obligation for obligation in obligations}
    sources_by_id = {
        source.source_id: source for source in (source_registry.sources if source_registry else [])
    }
    context: list[dict[str, object]] = []
    for obligation_id in control.obligation_ids:
        obligation = obligations_by_id.get(obligation_id)
        if obligation is None:
            continue
        source = sources_by_id.get(obligation.source_id)
        section = next(
            (item for item in source.sections if item.section_id == obligation.source_section),
            None,
        ) if source else None
        context.append(
            {
                "obligation_id": obligation.obligation_id,
                "statement": obligation.statement,
                "concepts": obligation.concepts,
                "applicability_expression": obligation.applicability_expression,
                "source_id": obligation.source_id,
                "source_section": obligation.source_section,
                "section": (
                    {
                        "section_id": section.section_id,
                        "title": section.title,
                        "text": section.text[:16000],
                        "location": section.location,
                        "page": section.page,
                        "page_end": section.page_end,
                    }
                    if section
                    else None
                ),
            }
        )
    return context


def _with_delivery_surface_facts(
    payload: dict[str, object],
    profile: ApplicabilityProfile,
    candidate_surfaces: set[Surface],
) -> dict[str, object]:
    confirmed = profile.confirmed_facts.get("evidence_surfaces")
    surface_facts = [
        {
            "surface": surface,
            "present": surface in profile.evidence_surfaces,
            "root": profile.roots.get(surface),
            "confirmation_source": confirmed.source if confirmed else "unresolved",
        }
        for surface in sorted(
            set(profile.evidence_surfaces) | set(profile.roots) | candidate_surfaces
        )
    ]
    return {**payload, "delivery_surface_facts": surface_facts}


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


def _source_refs_valid(
    provided: list[SourceRef],
    allowed: list[SourceRef],
    source_registry: SourceRegistry | None = None,
) -> bool:
    allowed_refs = {_ref_key(reference) for reference in allowed}
    if not all(_ref_key(reference) in allowed_refs for reference in provided):
        return False
    if source_registry is None:
        return True
    sources = {source.source_id: source for source in source_registry.sources}
    for reference in provided:
        if reference.source_id is None:
            continue
        source = sources.get(reference.source_id)
        if source is None:
            return False
        if reference.source_section and not any(
            section.section_id == reference.source_section for section in source.sections
        ):
            return False
    return True


def _allowed_source_refs(
    control: Control, obligations_by_id: dict[str, Obligation]
) -> list[SourceRef]:
    refs = [*control.source_refs]
    for obligation_id in control.obligation_ids:
        obligation = obligations_by_id.get(obligation_id)
        if obligation is not None:
            refs.extend(obligation.source_refs)
    return list({_ref_key(reference): reference for reference in refs}.values())


def _profile_refs_valid(
    provided: list[ProfileFactRef],
    profile: ApplicabilityProfile,
    trusted_sources: set[str] | None = None,
) -> bool:
    allowed_sources = trusted_sources or {"human_confirmed"}
    for reference in provided:
        fact = profile.confirmed_facts.get(reference.field_name)
        if (
            fact is None
            or fact.source not in allowed_sources
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
