from __future__ import annotations

from compliance_review.domain.models import (
    CoverageGateResult,
    CoverageImpact,
    DiffResult,
    RegressionComparison,
    ReusePlan,
    Snapshot,
)
from compliance_review.review.diff_report import render_diff_report
from compliance_review.review.diff_review import DiffReviewPlan


def test_terminal_not_required_unit_is_not_reported_as_missing_inheritance() -> None:
    unit_id = "cu.control.frontend_h5"
    partial_id = "cu.control.android_native"
    plan = DiffReviewPlan(
        diff=DiffResult(comparable=True),
        impacts=[
            CoverageImpact(
                coverage_unit_id=unit_id,
                affected=False,
                decision="unaffected",
                reasons=["no changed repository file for this reviewable surface"],
            ),
            CoverageImpact(
                coverage_unit_id=partial_id,
                affected=True,
                reasons=["baseline_anchor_hunk_overlap"],
            ),
        ],
        reuse=ReusePlan(
            complete=True,
            terminal_non_review_unit_ids=[unit_id],
            review_unit_ids=[partial_id],
        ),
        fingerprints={unit_id: "fingerprint", partial_id: "fingerprint"},
        review_work_item_ids=[],
    )
    snapshot = Snapshot(
        contract="compliance_snapshot.v1",
        run_id="run-current",
        git_revision="revision",
        mode="diff",
        semantic_baseline_run_id="full-a",
        baseline_run_id="diff-b",
        coverage_manifest_ref="coverage_manifest.json",
        applicability_hash="applicability-hash",
        ci_status="pass",
        run_status="completed",
        reviewed_partial_rows=[partial_id],
    )
    gate = CoverageGateResult(complete=True, ci_status="pass")
    regressions = RegressionComparison(
        current_run_id="run-current",
        ci_status="pass",
    )

    report = render_diff_report(snapshot, gate, plan, regressions)

    assert "无需执行（不要求/不适用）" in report
    assert "已重审（证据未完整）" in report
    assert "直接基线：`diff-b`" in report
    assert "语义 Full 基线：`full-a`" in report
    assert "未派发的 Coverage Gap：`0`" in report
    assert "缺少可继承结论" not in report
