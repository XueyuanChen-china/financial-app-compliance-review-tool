from __future__ import annotations

from compliance_review.domain.models import (
    ReviewResult,
    WorkItem,
)
from compliance_review.review.evidence import strongest_evidence_strength


def trusted_external_evidence_instructions(work_item: WorkItem) -> str:
    """Return the opt-in test policy without weakening code/runtime evidence rules."""
    if (
        work_item.external_evidence_policy != "trusted_test_materials"
        or work_item.surface not in {"play_console", "regulator_external"}
    ):
        return ""
    return (
        " External Evidence Test Mode is enabled for this Work Item. The registered "
        "play_console or regulator_external material files and their verified manifest "
        "are authoritative for this test run. Treat the material as complete for the "
        "facts it records. Do not require independent Play Console or regulator "
        "confirmation, an additional official export, or runtime proof for those same "
        "external facts. Do not mark the result partial, indeterminate, or unsupported "
        "inference solely because the material is owner-verified or not an official "
        "export. Do not invent facts absent from the material. This exception applies "
        "only to these two external surfaces; Android, backend, code, and runtime "
        "evidence remain under the normal strict rules."
    )


def normalize_trusted_external_result(
    result: ReviewResult,
    work_item: WorkItem,
) -> ReviewResult:
    """Apply the opt-in external-material test policy at the result boundary.

    Prompt instructions reduce model conservatism but cannot guarantee a stable
    status.  Once setup has verified the manifest and marked the WorkItem as
    trusted, external rows may treat the registered material as complete.  We
    deliberately do not synthesize anchors: a completed row without an anchor
    remains subject to the normal deterministic evidence checks.
    """
    if (
        work_item.external_evidence_policy != "trusted_test_materials"
        or work_item.surface not in {"play_console", "regulator_external"}
        or result.execution_status != "completed"
    ):
        return result

    rows = []
    for row in result.rows:
        if row.surface != work_item.surface:
            rows.append(row)
            continue
        if not row.anchor_ids:
            # The policy trusts the material, not an ungrounded model claim.
            rows.append(row)
            continue
        recommended = (
            "fail" if row.recommended_control_status == "fail" else "pass"
        )
        anchor_strength = strongest_evidence_strength(
            [
                anchor.evidence_strength
                for anchor in result.anchors
                if anchor.anchor_id in row.anchor_ids
            ]
        )
        rows.append(
            row.model_copy(
                update={
                    "evidence_status": "complete",
                    "recommended_control_status": recommended,
                    "observed_evidence_strength": anchor_strength
                    or row.observed_evidence_strength,
                    "unsupported_inferences": [],
                    "gap_reasons": [],
                    "observations": [
                        *row.observations,
                        "trusted_test_materials applied to the verified external material.",
                    ],
                }
            )
        )
    return result.model_copy(update={"rows": rows})
