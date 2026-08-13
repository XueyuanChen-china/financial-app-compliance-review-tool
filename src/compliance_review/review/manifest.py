from __future__ import annotations

from collections import defaultdict

from compliance_review.domain.models import (
    ApplicabilityProfile,
    Control,
    ControlSet,
    ReviewMode,
    Surface,
    WorkItem,
)
from compliance_review.review.applicability import control_applicability
from compliance_review.review.models import ExcludedControl, ReviewManifest


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
        grouped: dict[tuple[str, Surface], list[Control]] = defaultdict(list)
        excluded: list[ExcludedControl] = []
        for control in controls.controls:
            decision = control_applicability(control, profile)
            if decision is False:
                excluded.append(
                    ExcludedControl(
                        control_id=control.control_id,
                        reason="control applicability expression evaluated false",
                    )
                )
                continue
            for surface in control.required_surfaces:
                grouped[(control.module_id, surface)].append(control)

        work_items = [
            self._work_item(profile, module_id, surface, grouped[(module_id, surface)])
            for module_id, surface in sorted(grouped)
        ]
        return ReviewManifest(
            contract="review_manifest.v1",
            run_id=run_id,
            mode=mode,
            default_max_concurrency=max_concurrency,
            surface_roots={surface: root for surface, root in profile.roots.items()},
            work_items=work_items,
            excluded_controls=excluded,
            source_profile_version=profile.version,
            source_control_version=controls.version,
        )

    @staticmethod
    def _work_item(
        profile: ApplicabilityProfile,
        module_id: str,
        surface: Surface,
        controls: list[Control],
    ) -> WorkItem:
        control_ids = [control.control_id for control in controls]
        return WorkItem(
            work_item_id=f"wi.{module_id}.{surface}",
            module_id=module_id,
            surface=surface,
            control_ids=control_ids,
            # The scheduler mounts the surface root as the worker sandbox.
            # Worker paths are therefore relative to that mounted root.
            allowed_roots=["."],
            target_hints={
                "control_titles": [control.title for control in controls],
                "applicability": [control.applicability_expression for control in controls],
            },
            max_tool_rounds=12,
            max_files_read=20,
            max_lines_per_read=300,
        )
