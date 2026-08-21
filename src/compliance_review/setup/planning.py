from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from compliance_review.collectors.base import CollectorResult
from compliance_review.compilation.models import Obligation, SourceRegistry
from compliance_review.domain.models import (
    ApplicabilityCondition,
    ApplicabilityDiscoveryPlan,
    ApplicabilityDiscoveryResult,
    ApplicabilityDiscoveryResultSet,
    ApplicabilityDiscoveryWorkItem,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    ApplicabilitySet,
    ControlSet,
    CoverageSet,
    CoverageUnit,
    CoverageUnitStatus,
    DiscoveredProfileFact,
    DiscoveryTerminalStatus,
    ExternalEvidencePolicy,
    Surface,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.applicability import (
    ApplicabilityValidator,
    SemanticApplicabilityEvaluator,
    legacy_applicability_decision,
)
from compliance_review.review.provider import ModelProvider
from compliance_review.setup.models import AppFactSet, RepositoryInventory, WorkspaceMaterial

_ROOT_BACKED_SURFACES: set[Surface] = {
    "frontend_h5",
    "android_native",
    "backend_api_doc",
    "backend_code",
}
_DISCOVERABLE_FACT_KEYS = {
    "binary_options_trading_present",
    "offers_or_facilitates_loans",
    "earned_wage_access",
    "financial_features_present",
    "loan_application_flow_present",
    "account_deletion_flow_present",
    "regional_targeting_present",
    "sensitive_permission_use_present",
    "short_term_personal_loan_present",
}
_DISCOVERY_FACT_PATTERNS: dict[str, tuple[str, ...]] = {
    "binary_options_trading_present": ("binary option", "binary-options"),
    "earned_wage_access": ("earned wage access", "ewa"),
    "financial_features_present": ("financial feature", "financial product", "financial service"),
    "loan_application_flow_present": (
        "loan application",
        "apply for a loan",
        "apply for loan",
    ),
    "offers_or_facilitates_loans": (
        "personal loan",
        "loan app",
        "provides loans",
        "provide loans",
        "facilitates loans",
        "facilitate loans",
        "digital lending",
        "line of credit",
    ),
    "regional_targeting_present": (
        "targets a region",
        "targets a country",
        "regional targeting",
        "country targeting",
    ),
    "sensitive_permission_use_present": (
        "sensitive permission",
        "sensitive data permission",
        "contacts permission",
        "sms permission",
        "location permission",
    ),
    "short_term_personal_loan_present": (
        "short-term personal loan",
        "short term personal loan",
        "within 60 days",
        "60 days or less",
    ),
}
_SENSITIVE_ANDROID_PERMISSION_NAMES = {
    "ACCESS_BACKGROUND_LOCATION",
    "ACCESS_COARSE_LOCATION",
    "ACCESS_FINE_LOCATION",
    "GET_ACCOUNTS",
    "QUERY_ALL_PACKAGES",
    "READ_CALL_LOG",
    "READ_CONTACTS",
    "READ_EXTERNAL_STORAGE",
    "READ_MEDIA_IMAGES",
    "READ_MEDIA_VIDEO",
    "READ_PHONE_NUMBERS",
    "READ_PHONE_STATE",
    "READ_SMS",
    "RECEIVE_MMS",
    "RECEIVE_SMS",
    "SEND_SMS",
    "WRITE_CALL_LOG",
    "WRITE_CONTACTS",
    "WRITE_EXTERNAL_STORAGE",
}


@dataclass(frozen=True)
class WorkItemPlan:
    work_items: list[WorkItem]
    sandboxes: dict[str, RepositorySandbox]
    coverage: CoverageSet
    collector_results: dict[str, CollectorResult]


class ApplicabilityDiscoveryPlanner:
    """Create a deduplicated, persisted queue for unresolved applicability facts."""

    def plan(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        applicability: ApplicabilitySet,
        inventories: Sequence[RepositoryInventory],
        preparation_version: str = "applicability-prep-v2",
    ) -> ApplicabilityDiscoveryPlan:
        inventory_roots: dict[Surface, list[str]] = defaultdict(list)
        for inventory in inventories:
            surface = inventory.detected_surface or inventory.declared_surface
            if surface is not None:
                inventory_roots[surface].append(inventory.path)

        grouped: dict[
            tuple[
                tuple[str, ...],
                tuple[Surface, ...],
                tuple[tuple[Surface, tuple[str, ...]], ...],
            ],
            list[str],
        ] = defaultdict(list)
        terminal_gaps: dict[str, list[str]] = {}
        decision_by_id = {item.control_id: item for item in applicability.decisions}
        for control in controls.controls:
            decision = decision_by_id[control.control_id]
            if decision.decision != "unknown":
                continue
            candidate_fact_keys = _canonical_discovery_fact_keys(
                [
                    *decision.unresolved_conditions,
                    *_condition_fact_keys(control.applicability_condition),
                    control.title,
                    control.applicability_condition.reason or "",
                ]
            )
            fact_keys = sorted(candidate_fact_keys)
            if not fact_keys:
                terminal_gaps[control.control_id] = [
                    "Applicability is unknown but no bounded technical fact key was "
                    "identified; manual review required."
                ]
                continue
            allowed_surfaces = tuple(
                sorted(
                    {
                        surface
                        for surface in control.surface_candidates
                        if surface in _ROOT_BACKED_SURFACES and inventory_roots.get(surface)
                    }
                )
            )
            if not allowed_surfaces:
                terminal_gaps[control.control_id] = [
                    "No code-backed evidence surface is available for bounded discovery; "
                    "manual/external resolution required."
                ]
                continue
            roots_key = tuple(
                (surface, tuple(sorted(inventory_roots.get(surface, []))))
                for surface in allowed_surfaces
            )
            grouped[(tuple(fact_keys), allowed_surfaces, roots_key)].append(control.control_id)

        work_items: list[ApplicabilityDiscoveryWorkItem] = []
        for group_key, control_ids in sorted(grouped.items()):
            group_fact_keys, surface_tuple, roots_key = group_key
            identity = json.dumps(
                [group_fact_keys, surface_tuple, roots_key, preparation_version],
                sort_keys=True,
            )
            discovery_id = f"adw.{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
            work_items.append(
                ApplicabilityDiscoveryWorkItem(
                    discovery_id=discovery_id,
                    unresolved_fact_keys=list(group_fact_keys),
                    dependent_control_ids=sorted(control_ids),
                    allowed_surfaces=list(surface_tuple),
                    allowed_roots={surface: list(roots) for surface, roots in roots_key},
                    preparation_version=preparation_version,
                )
            )
        return ApplicabilityDiscoveryPlan(
            preparation_version=preparation_version,
            work_items=work_items,
            terminal_gaps=terminal_gaps,
        )


class ApplicabilityDiscoveryExecutor:
    """Resolve only bounded technical facts from deterministic Collector output.

    This is intentionally not a compliance reviewer.  A future Graphify-backed
    discovery subgraph can add verified facts, but it must return the same
    typed result contract and may not emit Control decisions.
    """

    def execute(
        self,
        plan: ApplicabilityDiscoveryPlan,
        facts: AppFactSet,
    ) -> ApplicabilityDiscoveryResultSet:
        fact_index = list(facts.facts)
        results: list[ApplicabilityDiscoveryResult] = []
        terminal_gaps = dict(plan.terminal_gaps)
        for work_item in plan.work_items:
            discovered: list["DiscoveredProfileFact"] = []
            for fact_key in work_item.unresolved_fact_keys:
                matches = [
                    fact
                    for fact in fact_index
                    if fact.source_surface in work_item.allowed_surfaces
                    and _fact_matches_discovery_key(fact_key, fact)
                    and _fact_is_within_roots(fact, work_item.allowed_roots)
                ]
                if matches and fact_key in {
                    "loan_application_flow_present",
                    "account_deletion_flow_present",
                    "sensitive_permission_use_present",
                }:
                    discovered.append(
                        DiscoveredProfileFact(
                            fact_key=fact_key,
                            value=True,
                            status="verified",
                            source_surface=matches[0].source_surface,
                            source_fact_ids=[fact.fact_id for fact in matches],
                            validator_outcome="accepted",
                            limitations=[
                                "Technical presence only; this does not establish legal "
                                "or licensing status."
                            ],
                        )
                    )
                elif matches:
                    discovered.append(
                        DiscoveredProfileFact(
                            fact_key=fact_key,
                            value=True,
                            status="candidate",
                            source_surface=matches[0].source_surface,
                            source_fact_ids=[fact.fact_id for fact in matches],
                            validator_outcome="candidate_only",
                            limitations=[
                                "Business/legal applicability still requires human or "
                                "external confirmation."
                            ],
                        )
                    )
                else:
                    source_surface = work_item.allowed_surfaces[0]
                    discovered.append(
                        DiscoveredProfileFact(
                            fact_key=fact_key,
                            value="unknown",
                            status="unresolved",
                            source_surface=source_surface,
                            validator_outcome="unresolved",
                            limitations=["No bounded deterministic fact supported this predicate."],
                        )
                    )
            statuses = {fact.status for fact in discovered}
            if statuses and statuses == {"verified"}:
                terminal_status: DiscoveryTerminalStatus = "resolved"
                reasons = ["All requested technical predicates were verified."]
            elif any(
                fact.fact_key in {"offers_or_facilitates_loans", "earned_wage_access"}
                for fact in discovered
            ):
                terminal_status = "manual_required"
                reasons = [
                    "Business or legal capability remains candidate-only; human/external "
                    "confirmation is required."
                ]
            else:
                terminal_status = "failed_exhausted"
                reasons = ["Bounded technical discovery did not resolve every predicate."]
            result = ApplicabilityDiscoveryResult(
                discovery_id=work_item.discovery_id,
                terminal_status=terminal_status,
                facts=discovered,
                dependent_control_ids=work_item.dependent_control_ids,
                reasons=reasons,
            )
            results.append(result)
            if terminal_status != "resolved":
                for control_id in work_item.dependent_control_ids:
                    terminal_gaps.setdefault(control_id, []).extend(reasons)
        return ApplicabilityDiscoveryResultSet(
            preparation_version=plan.preparation_version,
            results=results,
            terminal_gaps=terminal_gaps,
            barrier_complete=True,
        )

    @staticmethod
    def apply_verified_facts(
        profile: ApplicabilityProfile,
        results: ApplicabilityDiscoveryResultSet,
    ) -> ApplicabilityProfile:
        """Apply verified technical facts without turning candidates into truth."""

        confirmed = dict(profile.confirmed_facts)
        for result in results.results:
            for fact in result.facts:
                if fact.status == "verified" and fact.validator_outcome == "accepted":
                    confirmed[fact.fact_key] = ApplicabilityProfileFact(
                        value=fact.value,
                        source="deterministic",
                    )
        return profile.model_copy(update={"confirmed_facts": confirmed})


class ApplicabilityEngine:
    """Build a complete, validated applicability ledger for every Control."""

    def __init__(
        self,
        provider: ModelProvider | None = None,
        source_registry: SourceRegistry | None = None,
        obligations: list[Obligation] | None = None,
    ) -> None:
        self.provider = provider
        self.source_registry = source_registry
        self.obligations = obligations or []

    def evaluate(self, profile: ApplicabilityProfile, controls: ControlSet) -> ApplicabilitySet:
        if self.provider is None:
            decisions = [
                legacy_applicability_decision(control, profile) for control in controls.controls
            ]
        else:
            try:
                decisions = SemanticApplicabilityEvaluator(self.provider).evaluate(
                    profile,
                    controls,
                    source_registry=self.source_registry,
                    obligations=self.obligations,
                )
            except (OSError, TypeError, ValueError):
                # The semantic call is advisory. A transport or schema failure must
                # never make controls disappear from the deterministic denominator.
                decisions = [
                    legacy_applicability_decision(control, profile).model_copy(
                        update={
                            "decision": "unknown",
                            "reason": (
                                "semantic applicability evaluation failed; retained conservatively"
                            ),
                            "unresolved_conditions": ["semantic_applicability_unavailable"],
                            "confidence": "low",
                        }
                    )
                    for control in controls.controls
                ]
        decisions = ApplicabilityValidator().validate(
            profile,
            controls,
            decisions,
            obligations=self.obligations,
            source_registry=self.source_registry,
        )
        excluded = [item.control_id for item in decisions if item.decision == "not_applicable"]
        unknown = [item.control_id for item in decisions if item.decision == "unknown"]
        return ApplicabilitySet(
            contract="applicability_set.v2",
            profile_version=profile.version,
            control_version=controls.version,
            decisions=decisions,
            excluded_control_ids=excluded,
            unknown_control_ids=unknown,
        )


def _fact_matches_discovery_key(fact_key: str, fact: object) -> bool:
    """Match only deterministic technical signals, never legal conclusions."""

    fact_type = getattr(fact, "fact_type", "")
    text = _fact_text(fact).lower()
    if fact_key == "sensitive_permission_use_present":
        permission_name = str(getattr(fact, "observed_value", "")).rsplit(".", 1)[-1]
        return (
            fact_type == "android_manifest_permission"
            and permission_name in _SENSITIVE_ANDROID_PERMISSION_NAMES
        )
    if fact_key == "loan_application_flow_present":
        return any(
            token in text
            for token in ("loan", "borrow", "application", "apply", "credit")
        ) and fact_type in {
            "declared_api_endpoint",
            "repository_detection_signal",
            "api_document_availability",
            "backend_presence",
        }
    if fact_key == "account_deletion_flow_present":
        return any(token in text for token in ("delete", "deactivate", "close account"))
    if fact_key == "earned_wage_access":
        return "earned wage" in text or "ewa" in text
    if fact_key == "offers_or_facilitates_loans":
        return "loan" in text or "credit" in text
    return False


def _fact_text(fact: object) -> str:
    observed = getattr(fact, "observed_value", "")
    return f"{getattr(fact, 'fact_type', '')} {observed}"


def _fact_is_within_roots(fact: object, allowed_roots: dict[Surface, list[str]]) -> bool:
    source_surface = getattr(fact, "source_surface", None)
    roots = next(
        (candidate for surface, candidate in allowed_roots.items() if surface == source_surface),
        [],
    )
    if not roots:
        return True
    for source_ref in getattr(fact, "source_refs", []):
        path = source_ref.path or ""
        if any(path.startswith(root) or root in {".", ""} for root in roots):
            return True
    return False


class CoverageUnitBuilder:
    """Build the immutable Control x Required Surface coverage denominator."""

    def build(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        applicability: ApplicabilitySet,
        available_surfaces: set[Surface] | None = None,
    ) -> CoverageSet:
        if available_surfaces is None:
            available_surfaces = set(profile.evidence_surfaces)
        decision_by_control = {item.control_id: item for item in applicability.decisions}
        units: list[CoverageUnit] = []
        missing_surfaces: set[Surface] = set()
        uncovered_control_ids: list[str] = []
        for control in controls.controls:
            decision = decision_by_control[control.control_id]
            if control.evidence_claims:
                route_by_id = {
                    route.route_id: (claim, route)
                    for claim in control.evidence_claims
                    for route in claim.proof_routes
                }
                for route_id in decision.selected_route_ids:
                    claim_route = route_by_id.get(route_id)
                    if claim_route is None or decision.decision == "not_applicable":
                        continue
                    claim, route = claim_route
                    route_applicability_unknown = route_id in decision.unknown_route_ids
                    requirement_available = route.surface in available_surfaces
                    if route.execution_mode == "manual_or_external":
                        claim_coverage_status: CoverageUnitStatus = "manual_required"
                        reason = "selected proof route requires manual or external evidence"
                    elif not requirement_available:
                        claim_coverage_status = "missing_surface"
                        missing_surfaces.add(route.surface)
                        reason = (
                            f"selected proof route requires surface {route.surface}, but no "
                            "registered source is available"
                        )
                    elif decision.decision == "unknown" or route_applicability_unknown:
                        claim_coverage_status = "unknown_applicability"
                        reason = (
                            "selected proof route applicability is unknown; retained for "
                            "bounded review"
                            if route_applicability_unknown
                            else "control applicability is unknown; selected proof route "
                            "retained for bounded review"
                        )
                    else:
                        claim_coverage_status = "planned"
                        reason = "Applicability selected this proof route for the confirmed profile"
                    requirement = control.evidence_requirements.get(route.surface)
                    unit_id = f"cu.{control.control_id}.{claim.claim_id}.{route.route_id}"
                    units.append(
                        CoverageUnit(
                            coverage_unit_id=unit_id,
                            control_id=control.control_id,
                            module_id=control.module_id,
                            surface=route.surface,
                            applicability_status=decision.decision,
                            coverage_status=claim_coverage_status,
                            required_evidence_strength=route.expected_evidence_strength,
                            reason=reason,
                            evidence_requirement_rationale=route.claim_to_prove,
                            obligation_ids=list(claim.obligation_ids),
                            source_refs=list(claim.source_refs),
                            evidence_requirement_ids=(
                                [
                                    requirement.requirement_id
                                    or f"req.{control.control_id}.{route.surface}"
                                ]
                                if requirement is not None
                                else []
                            ),
                            claim_id=claim.claim_id,
                            route_id=route.route_id,
                            claim_statement=claim.statement,
                            proof_scope=route.claim_to_prove,
                            acceptance_criteria=list(route.acceptance_criteria),
                            proof_limits=list(route.proof_limits),
                            execution_mode=route.execution_mode,
                        )
                        )
                if decision.decision != "not_applicable" and not decision.selected_route_ids:
                    uncovered_control_ids.append(control.control_id)
                # A selected route is the complete denominator for a claim. Do
                # not fall back to all policy candidate surfaces for new controls.
                continue
            surface_requirements = {
                item.surface: item for item in decision.surface_requirements
            }
            resolved_required_surfaces = set(
                decision.resolved_required_surfaces
                or [
                    item.surface
                    for item in decision.surface_requirements
                    if item.decision == "required"
                ]
            )
            for surface in control.surface_candidates:
                requirement = control.evidence_requirements.get(surface)
                requirement_rationale = (
                    requirement.rationale
                    if requirement is not None
                    else "No evidence requirement rationale recorded."
                )
                if decision.decision == "not_applicable":
                    units.append(
                        CoverageUnit(
                            coverage_unit_id=f"cu.{control.control_id}.{surface}",
                            control_id=control.control_id,
                            module_id=control.module_id,
                            surface=surface,
                            applicability_status=decision.decision,
                            coverage_status="not_applicable",
                            required_evidence_strength=control.minimum_evidence_strength[surface],
                            reason="control applicability evaluated not_applicable",
                            evidence_requirement_rationale=requirement_rationale,
                            obligation_ids=(requirement.obligation_ids if requirement else []),
                            source_refs=(requirement.source_refs if requirement else []),
                        )
                    )
                    continue
                coverage_status: CoverageUnitStatus
                requirement_decision = surface_requirements.get(surface)
                if requirement_decision is None or requirement_decision.decision == "unknown":
                    coverage_status = "unknown_applicability"
                    reason = (
                        "control applicability is unknown; retained conservatively"
                    )
                elif surface not in resolved_required_surfaces:
                    coverage_status = "not_required"
                    reason = (
                        requirement_decision.reason
                        or "Applicability resolved this candidate surface as not required"
                    )
                elif decision.decision == "unknown":
                    # The control-level conclusion remains conservative, but a
                    # resolved surface can still be investigated. This avoids
                    # turning a confirmed absent H5 surface into a false gap.
                    coverage_status = "unknown_applicability"
                    reason = (
                        "control applicability is unknown; required surface retained for "
                        "bounded review"
                    )
                else:
                    coverage_status = "planned"
                    reason = (
                        "Control declares this surface as an unconditional evidence requirement"
                    )
                surface_available = surface in available_surfaces
                if coverage_status == "planned" and not surface_available:
                    coverage_status = "missing_surface"
                    missing_surfaces.add(surface)
                    reason = (
                        f"required evidence source for surface {surface} is not registered "
                        "or does not exist"
                    )
                units.append(
                    CoverageUnit(
                        coverage_unit_id=f"cu.{control.control_id}.{surface}",
                        control_id=control.control_id,
                        module_id=control.module_id,
                        surface=surface,
                        applicability_status=decision.decision,
                        coverage_status=coverage_status,
                        required_evidence_strength=control.minimum_evidence_strength[surface],
                        reason=reason,
                        evidence_requirement_rationale=requirement_rationale,
                        obligation_ids=(requirement.obligation_ids if requirement else []),
                        source_refs=(requirement.source_refs if requirement else []),
                        evidence_requirement_ids=[
                            requirement.requirement_id
                            or f"req.{control.control_id}.{surface}"
                        ]
                        if requirement is not None
                        else [],
                    )
                )
        return CoverageSet(
            contract="coverage_set.v2",
            profile_version=profile.version,
            control_version=controls.version,
            units=units,
            excluded_control_ids=list(applicability.excluded_control_ids),
            unknown_control_ids=list(applicability.unknown_control_ids),
            uncovered_control_ids=sorted(uncovered_control_ids),
            missing_surfaces=sorted(missing_surfaces),
        )
class WorkItemPlanner:
    """Create one bounded WorkItem for each reviewable Control x Surface unit."""

    def plan(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        coverage: CoverageSet,
        facts: AppFactSet,
        inventories: Sequence[RepositoryInventory],
        run_root: Path,
        materials: Sequence[WorkspaceMaterial] = (),
        external_evidence_policy: ExternalEvidencePolicy = "strict",
    ) -> WorkItemPlan:
        controls_by_id = {control.control_id: control for control in controls.controls}
        collector_results = _collector_results(facts)
        repositories_by_surface: dict[Surface, list[RepositoryInventory]] = defaultdict(list)
        for inventory in inventories:
            inventory_surface = inventory.detected_surface or inventory.declared_surface
            if inventory_surface is not None:
                repositories_by_surface[inventory_surface].append(inventory)
        materials_by_surface: dict[Surface, list[WorkspaceMaterial]] = defaultdict(list)
        for material in materials:
            if material.surface is not None:
                materials_by_surface[material.surface].append(material)

        def has_evidence_source(surface: Surface) -> bool:
            return bool(
                repositories_by_surface.get(surface)
                or [
                    item
                    for item in materials_by_surface.get(surface, [])
                    if Path(item.path).exists()
                ]
            )

        sandboxes: dict[str, RepositorySandbox] = {}
        work_items: list[WorkItem] = []
        assigned: dict[str, str] = {}
        trusted_external_surfaces = _verified_external_material_surfaces(
            materials, external_evidence_policy
        )
        reviewable_units = sorted(
            (
                unit
                for unit in coverage.units
                if (
                    unit.coverage_status == "planned" and has_evidence_source(unit.surface)
                ) or (
                    unit.coverage_status == "unknown_applicability"
                    and has_evidence_source(unit.surface)
                )
            ),
            key=lambda item: item.coverage_unit_id,
        )
        for unit in reviewable_units:
            module_id = unit.module_id
            surface = unit.surface
            units = [unit]
            repositories = repositories_by_surface.get(surface, [])
            material_sources = [
                item
                for item in materials_by_surface.get(surface, [])
                if Path(item.path).expanduser().exists()
            ]
            if repositories:
                repository_ids = [inventory.repo_id for inventory in repositories]
                sandbox_root = _sandbox_root(repositories)
                allowed_roots = [
                    _relative_root(sandbox_root, Path(inventory.path)) for inventory in repositories
                ]
                repository_id = repository_ids[0] if len(repository_ids) == 1 else "workspace"
                id_prefix = repository_id if len(repository_ids) == 1 else "workspace"
                source_families: list[str] = []
            elif material_sources:
                source_families = sorted({item.source_family for item in material_sources})
                sandbox_root, allowed_roots = _material_scope(material_sources)
                repository_ids = ["workspace"]
                repository_id = "workspace"
                id_prefix = "material"
            else:
                repository_ids = ["workspace"]
                repository_id = "workspace"
                id_prefix = "workspace"
                sandbox_root = run_root / "surface_inputs" / surface
                sandbox_root.mkdir(parents=True, exist_ok=True)
                allowed_roots = ["."]
                source_families = []
            sandbox = RepositorySandbox(sandbox_root)
            safe_repository_id = _safe_identifier(id_prefix)
            safe_control_id = _safe_identifier(unit.control_id)
            work_item_id = f"wi.{safe_repository_id}.{safe_control_id}.{surface}"
            if unit.claim_id or unit.route_id:
                safe_claim_id = _safe_identifier(unit.claim_id or "claim")
                safe_route_id = _safe_identifier(unit.route_id or "route")
                work_item_id = f"{work_item_id}.{safe_claim_id}.{safe_route_id}"
            if work_item_id in sandboxes:
                raise ValueError(
                    "duplicate WorkItem identity for coverage unit "
                    f"{unit.coverage_unit_id}: {work_item_id}"
                )
            sandboxes[work_item_id] = sandbox
            control_list = [controls_by_id[unit.control_id] for unit in units]
            repository_id_set = set(repository_ids)
            fact_refs = sorted(
                fact.fact_id
                for result in collector_results.values()
                if result.source_surface == surface
                and (repository_id == "workspace" or result.repo_id in repository_id_set)
                for fact in result.facts
            )
            work_items.append(
                WorkItem(
                    work_item_id=work_item_id,
                    module_id=module_id,
                    repository_id=repository_id,
                    repository_ids=repository_ids,
                    surface=surface,
                    external_evidence_policy=(
                        "trusted_test_materials"
                        if surface in trusted_external_surfaces
                        else "strict"
                    ),
                    control_ids=[unit.control_id for unit in units],
                    coverage_unit_ids=[unit.coverage_unit_id for unit in units],
                    acceptance_criteria=[
                        criterion
                        for unit in units
                        for criterion in unit.acceptance_criteria
                    ],
                    evidence_requirement_ids=list(unit.evidence_requirement_ids),
                    resolved_context={
                        "jurisdiction": profile.jurisdiction,
                        "business_type": list(profile.business_type),
                        "self_lending": profile.self_lending,
                        "evidence_surfaces": list(profile.evidence_surfaces),
                        "applicability_status": unit.applicability_status,
                        "applicability_reason": unit.reason,
                        "claim_id": unit.claim_id,
                        "claim_statement": unit.claim_statement,
                        "route_id": unit.route_id,
                        "proof_scope": unit.proof_scope,
                        "acceptance_criteria": [
                            criterion.model_dump(mode="json")
                            for unit in units
                            for criterion in unit.acceptance_criteria
                        ],
                        "proof_limits": list(unit.proof_limits),
                        "profile_fact_refs": sorted(profile.confirmed_facts),
                    },
                    collector_fact_refs=fact_refs,
                    allowed_roots=allowed_roots,
                    target_hints={
                        "control_titles": [control.title for control in control_list],
                        "applicability": [
                            json.dumps(
                                control.applicability_condition.model_dump(mode="json"),
                                sort_keys=True,
                            )
                            for control in control_list
                        ],
                        "coverage_status": [unit.coverage_status for unit in units],
                        "review_purpose": [
                            (
                                "bounded_applicability_investigation"
                                if unit.coverage_status == "unknown_applicability"
                                else "compliance_evidence_review"
                            )
                            for unit in units
                        ],
                        "required_evidence_strength": [
                            f"{unit.control_id}:{unit.required_evidence_strength}" for unit in units
                        ],
                        "evidence_requirement_ids": list(unit.evidence_requirement_ids),
                        "evidence_requirement_rationale": [
                            unit.evidence_requirement_rationale for unit in units
                        ],
                        "claim_ids": [unit.claim_id for unit in units if unit.claim_id],
                        "claim_statements": [
                            unit.claim_statement for unit in units if unit.claim_statement
                        ],
                        "proof_limits": [
                            limit for unit in units for limit in unit.proof_limits
                        ],
                        "repository_ids": repository_ids,
                        "evidence_source_kind": [
                            "code_repository" if repositories else "workspace_material"
                        ],
                        # These paths are relative to the Work Item sandbox and are
                        # therefore directly usable by read_file/list_files.
                        "material_paths": allowed_roots if not repositories else [],
                        "material_source_families": source_families,
                        "external_evidence_policy": [
                            (
                                "trusted_test_materials"
                                if surface in trusted_external_surfaces
                                else "strict"
                            )
                        ],
                    },
                    max_tool_rounds=12,
                    max_files_read=20,
                    max_lines_per_read=300,
                )
            )
            for unit in units:
                assigned[unit.coverage_unit_id] = work_item_id

        updated_units = [
            unit.model_copy(update={"work_item_id": assigned.get(unit.coverage_unit_id)})
            for unit in coverage.units
        ]
        return WorkItemPlan(
            work_items=work_items,
            sandboxes=sandboxes,
            coverage=coverage.model_copy(update={"units": updated_units}),
            collector_results=collector_results,
        )


def _verified_external_material_surfaces(
    materials: Sequence[WorkspaceMaterial],
    requested_policy: ExternalEvidencePolicy,
) -> set[Surface]:
    """Enable trusted test evidence only when its manifest explicitly allows it."""
    if requested_policy != "trusted_test_materials":
        return set()
    manifest_paths = {
        Path(material.path).expanduser().resolve()
        for material in materials
        if Path(material.path).name == "external_materials_manifest.json"
    }
    verified: set[Surface] = set()
    for path in manifest_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        if (
            payload.get("material_status") != "verified_external_materials"
            or payload.get("not_validated_as_official") is not False
        ):
            continue
        for material in payload.get("materials", []):
            if not isinstance(material, dict):
                continue
            if material.get("verification_status") not in {
                "verified",
                "verified_by_owner",
            }:
                continue
            surface = material.get("surface")
            if surface in {"play_console", "regulator_external"}:
                verified.add(surface)
    return verified


def _material_scope(materials: Sequence[WorkspaceMaterial]) -> tuple[Path, list[str]]:
    """Build the narrowest readable sandbox for registered material paths."""
    paths = [Path(item.path).expanduser().resolve() for item in materials]
    roots = [path if path.is_dir() else path.parent for path in paths]
    sandbox_root = Path(os.path.commonpath([root.as_posix() for root in roots]))
    allowed_roots: list[str] = []
    for path in paths:
        target = path if path.is_dir() else path
        relative = target.relative_to(sandbox_root).as_posix()
        allowed_roots.append(relative or ".")
    return sandbox_root, sorted(set(allowed_roots))


def _collector_results(facts: AppFactSet) -> dict[str, CollectorResult]:
    results = [CollectorResult.model_validate(item) for item in facts.collector_results]
    return {
        f"{item.repo_id or 'workspace'}/{item.collector_id}/{index}": item
        for index, item in enumerate(results, start=1)
    }


def _sandbox_root(repositories: Sequence[RepositoryInventory]) -> Path:
    paths = [Path(repository.path).expanduser().resolve() for repository in repositories]
    return Path(os.path.commonpath([path.as_posix() for path in paths]))


def _relative_root(root: Path, repository: Path) -> str:
    relative = repository.resolve().relative_to(root.resolve()).as_posix()
    return relative or "."


def _safe_identifier(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe or "item"


def _condition_fact_keys(condition: ApplicabilityCondition) -> list[str]:
    if condition.kind == "atom":
        return [condition.fact] if condition.fact else []
    return sorted(
        {
            fact_key
            for child in condition.conditions
            for fact_key in _condition_fact_keys(child)
        }
    )


def _canonical_discovery_fact_keys(values: Sequence[str]) -> set[str]:
    """Normalize model prose and structured facts into a bounded fact vocabulary."""

    keys: set[str] = set()
    for value in values:
        normalized = value.strip().lower()
        if not normalized:
            continue
        if normalized in _DISCOVERABLE_FACT_KEYS:
            keys.add(normalized)
        for fact_key, patterns in _DISCOVERY_FACT_PATTERNS.items():
            if any(pattern in normalized for pattern in patterns):
                keys.add(fact_key)
    return keys
