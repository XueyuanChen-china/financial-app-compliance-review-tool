from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from compliance_review.collectors.base import CollectorResult
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
    sandboxes: dict[str, RepositorySandbox]
    coverage: CoverageSet
    collector_results: dict[str, CollectorResult]


class ApplicabilityEngine:
    """Evaluate every Control with the finite, non-eval applicability DSL."""

    def evaluate(self, profile: ApplicabilityProfile, controls: ControlSet) -> ApplicabilitySet:
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
            for surface in control.required_surfaces:
                if decision.status == "false":
                    units.append(
                        CoverageUnit(
                            coverage_unit_id=f"cu.{control.control_id}.{surface}",
                            control_id=control.control_id,
                            module_id=control.module_id,
                            surface=surface,
                            applicability_status=decision.status,
                            coverage_status="not_applicable",
                            required_evidence_strength=control.minimum_evidence_strength[surface],
                            reason="control applicability evaluated false",
                        )
                    )
                    continue
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
    """Group Coverage Units by Module x Surface and preserve repository roots."""

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
            if unit.coverage_status != "planned":
                continue
            grouped[(unit.module_id, unit.surface)].append(unit)

        collector_results = _collector_results(facts)
        repositories_by_surface: dict[Surface, list[RepositoryInventory]] = defaultdict(list)
        for inventory in inventories:
            inventory_surface = inventory.detected_surface or inventory.declared_surface
            if inventory_surface is not None:
                repositories_by_surface[inventory_surface].append(inventory)

        sandboxes: dict[str, RepositorySandbox] = {}
        work_items: list[WorkItem] = []
        assigned: dict[str, str] = {}
        for module_id, surface in sorted(grouped):
            units = sorted(grouped[(module_id, surface)], key=lambda item: item.coverage_unit_id)
            repositories = repositories_by_surface.get(surface, [])
            if repositories:
                repository_ids = [inventory.repo_id for inventory in repositories]
                sandbox_root = _sandbox_root(repositories)
                allowed_roots = [
                    _relative_root(sandbox_root, Path(inventory.path)) for inventory in repositories
                ]
                repository_id = repository_ids[0] if len(repository_ids) == 1 else "workspace"
                id_prefix = repository_id if len(repository_ids) == 1 else "workspace"
            else:
                repository_ids = ["workspace"]
                repository_id = "workspace"
                id_prefix = "workspace"
                sandbox_root = run_root / "surface_inputs" / surface
                sandbox_root.mkdir(parents=True, exist_ok=True)
                allowed_roots = ["."]
            sandbox = RepositorySandbox(sandbox_root)
            safe_module_id = _safe_identifier(module_id)
            safe_repository_id = _safe_identifier(id_prefix)
            work_item_id = f"wi.{safe_repository_id}.{safe_module_id}.{surface}"
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
                    control_ids=[unit.control_id for unit in units],
                    coverage_unit_ids=[unit.coverage_unit_id for unit in units],
                    collector_fact_refs=fact_refs,
                    allowed_roots=allowed_roots,
                    target_hints={
                        "control_titles": [control.title for control in control_list],
                        "applicability": [
                            control.applicability_expression for control in control_list
                        ],
                        "coverage_status": [unit.coverage_status for unit in units],
                        "required_evidence_strength": [
                            f"{unit.control_id}:{unit.required_evidence_strength}" for unit in units
                        ],
                        "repository_ids": repository_ids,
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
