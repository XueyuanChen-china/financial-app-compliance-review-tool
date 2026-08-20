from __future__ import annotations

import json
from typing import Any

from compliance_review.domain.models import (
    ApplicabilityProfile,
    Control,
    ControlSet,
    DiffReviewWorkItem,
    FullReviewWorkItem,
    ReviewMode,
    Surface,
    WorkItem,
)
from compliance_review.review.models import ReviewManifest


class ReviewManifestBuilder:
    """Build deterministic module-by-surface work items from controls and profile."""

    def build(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        run_id: str,
        mode: ReviewMode = "full",
        max_concurrency: int = 3,
    ) -> ReviewManifest:
        work_items = [
            self._work_item(profile, control, surface)
            for control in sorted(controls.controls, key=lambda item: item.control_id)
            for surface in control.surface_candidates
        ]
        return ReviewManifest(
            contract="review_manifest.v1",
            run_id=run_id,
            mode=mode,
            default_max_concurrency=max_concurrency,
            surface_roots={surface: root for surface, root in profile.roots.items()},
            work_items=work_items,
            # This legacy direct-manifest path has no semantic applicability
            # provider, so it must retain all controls conservatively.
            excluded_controls=[],
            source_profile_version=profile.version,
            source_control_version=controls.version,
        )

    @staticmethod
    def _work_item(
        profile: ApplicabilityProfile,
        control: Control,
        surface: Surface,
    ) -> WorkItem:
        requirement = control.evidence_requirements.get(surface)
        return WorkItem(
            work_item_id=f"wi.{control.control_id}.{surface}",
            module_id=control.module_id,
            surface=surface,
            control_ids=[control.control_id],
            coverage_unit_ids=[f"cu.{control.control_id}.{surface}"],
            evidence_requirement_ids=(
                [requirement.requirement_id or f"req.{control.control_id}.{surface}"]
                if requirement is not None
                else []
            ),
            # The scheduler mounts the surface root as the worker sandbox.
            # Worker paths are therefore relative to that mounted root.
            allowed_roots=["."],
            target_hints={
                "control_titles": [control.title],
                "applicability": [
                    json.dumps(
                        control.applicability_condition.model_dump(mode="json"), sort_keys=True
                    )
                ],
                "evidence_requirement_rationale": (
                    [requirement.rationale] if requirement is not None else []
                ),
            },
            max_tool_rounds=12,
            max_files_read=20,
            max_lines_per_read=300,
        )


class ReviewWorkItemBuilder:
    """Construct mode-specific immutable reviewer input from one CoverageUnit."""

    def build_full(self, item: WorkItem) -> FullReviewWorkItem:
        payload = item.model_dump(mode="json")
        payload.update({"mode": "full", "baseline_context": None, "change_context": None})
        return FullReviewWorkItem.model_validate(payload)

    def build_diff(
        self,
        item: WorkItem,
        *,
        baseline_context: dict[str, Any],
        change_context: dict[str, Any],
    ) -> DiffReviewWorkItem:
        payload = item.model_dump(mode="json")
        payload.update(
            {
                "mode": "diff",
                "baseline_context": baseline_context,
                "change_context": change_context,
            }
        )
        return DiffReviewWorkItem.model_validate(payload)
