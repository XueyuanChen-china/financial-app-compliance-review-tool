from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from compliance_review.domain.models import (
    ApplicabilityDecision,
    ApplicabilityProfile,
    ApplicabilitySet,
    ControlSet,
    CoverageSet,
    CoverageUnit,
    Surface,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.applicability import control_applicability
from compliance_review.setup.models import AppFactSet, RepositoryInventory

_ROOT_BACKED_SURFACES: set[Surface] = {
    "frontend_h5",
    "android_native",
    "backend_api_doc",
    "backend_code",
}


@dataclass(frozen=True)
class WorkItemPlan:
    work_items: list[WorkItem]
    sandboxes: dict[Surface, RepositorySandbox]
    coverage: CoverageSet


class ApplicabilityEngine:
    """Evaluate every Control with the finite, non-eval applicability DSL."""

    def evaluate(
        self, profile: ApplicabilityProfile, controls: ControlSet
    ) -> ApplicabilitySet:
        decisions: list[ApplicabilityDecision] = []
        excluded: list[str] = []
        unknown: list[str] = []
        for control in controls.controls:
            result = control_applicability(control, profile)
            if result is True:
                status: Literal["true", "false", "unknown"] = "true"
                reason = "applicability expression evaluated true"
            elif result is False:
                status = "false"
                reason = "applicability expression evaluated false"
                excluded.append(control.control_id)
            else:
                status = "unknown"
                reason = (
                    "applicability expression or required profile value is unknown; "
                    "control is retained conservatively"
                )
                unknown.append(control.control_id)
            decisions.append(
                ApplicabilityDecision(
                    control_id=control.control_id,
                    expression=control.applicability_expression,
                    status=status,
                    reason=reason,
                )
            )
        return ApplicabilitySet(
            profile_version=profile.version,
            control_version=controls.version,
            decisions=decisions,
            excluded_control_ids=excluded,
            unknown_control_ids=unknown,
        )


class CoverageUnitBuilder:
    """Build the immutable Control x Required Surface coverage denominator."""

    def build(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        applicability: ApplicabilitySet,
    ) -> CoverageSet:
        decision_by_control = {item.control_id: item for item in applicability.decisions}
        units: list[CoverageUnit] = []
        missing_surfaces: set[Surface] = set()
        for control in controls.controls:
            decision = decision_by_control[control.control_id]
            if decision.status == "false":
                continue
            for surface in control.required_surfaces:
                surface_available = surface in profile.evidence_surfaces and (
                    surface not in _ROOT_BACKED_SURFACES or surface in profile.roots
                )
                if not surface_available:
                    coverage_status: Literal[
                        "planned", "missing_surface", "unknown_applicability"
                    ] = "missing_surface"
                    missing_surfaces.add(surface)
                    reason = (
                        f"required surface {surface} is not present in the confirmed "
                        "AppProfile evidence_surfaces"
                    )
                elif decision.status == "unknown":
                    coverage_status = "unknown_applicability"
                    reason = "control retained because applicability is unknown"
                else:
                    coverage_status = "planned"
                    reason = "applicable control and required surface are in scope"
                units.append(
                    CoverageUnit(
                        coverage_unit_id=f"cu.{control.control_id}.{surface}",
                        control_id=control.control_id,
                        module_id=control.module_id,
                        surface=surface,
                        applicability_status=decision.status,
                        coverage_status=coverage_status,
                        required_evidence_strength=control.minimum_evidence_strength[surface],
                        reason=reason,
                    )
                )
        return CoverageSet(
            profile_version=profile.version,
            control_version=controls.version,
            units=units,
            excluded_control_ids=list(applicability.excluded_control_ids),
            unknown_control_ids=list(applicability.unknown_control_ids),
            missing_surfaces=sorted(missing_surfaces),
        )


class WorkItemPlanner:
    """Group Coverage Units by Module x Surface without changing the denominator."""

    def plan(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        coverage: CoverageSet,
        facts: AppFactSet,
        inventories: Sequence[RepositoryInventory],
        run_root: Path,
    ) -> WorkItemPlan:
        controls_by_id = {control.control_id: control for control in controls.controls}
        grouped: dict[tuple[str, Surface], list[CoverageUnit]] = defaultdict(list)
        for unit in coverage.units:
            grouped[(unit.module_id, unit.surface)].append(unit)

        roots = self._surface_roots(profile, inventories)
        sandboxes: dict[Surface, RepositorySandbox] = {}
        work_items: list[WorkItem] = []
        assigned: dict[str, str] = {}
        for module_id, surface in sorted(grouped):
            units = sorted(grouped[(module_id, surface)], key=lambda item: item.coverage_unit_id)
            surface_root = roots.get(surface)
            if surface_root is None:
                surface_root = run_root / "surface_inputs" / surface
                surface_root.mkdir(parents=True, exist_ok=True)
            sandbox = RepositorySandbox(Path(surface_root))
            sandboxes[surface] = sandbox
            control_list = [controls_by_id[unit.control_id] for unit in units]
            fact_refs = sorted(
                fact.fact_id for fact in facts.facts if fact.source_surface == surface
            )
            work_item_id = f"wi.{module_id}.{surface}"
            work_items.append(
                WorkItem(
                    work_item_id=work_item_id,
                    module_id=module_id,
                    surface=surface,
                    control_ids=[unit.control_id for unit in units],
                    coverage_unit_ids=[unit.coverage_unit_id for unit in units],
                    collector_fact_refs=fact_refs,
                    allowed_roots=["."],
                    target_hints={
                        "control_titles": [control.title for control in control_list],
                        "applicability": [
                            control.applicability_expression for control in control_list
                        ],
                        "coverage_status": [unit.coverage_status for unit in units],
                        "required_evidence_strength": [
                            f"{unit.control_id}:{unit.required_evidence_strength}"
                            for unit in units
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
        )

    @staticmethod
    def _surface_roots(
        profile: ApplicabilityProfile, inventories: Sequence[RepositoryInventory]
    ) -> dict[Surface, Path]:
        roots: dict[Surface, Path] = {
            surface: Path(root).expanduser().resolve()
            for surface, root in profile.roots.items()
        }
        for inventory in inventories:
            surface = inventory.detected_surface or inventory.declared_surface
            if surface is not None:
                roots.setdefault(surface, Path(inventory.path).expanduser().resolve())
        return roots
