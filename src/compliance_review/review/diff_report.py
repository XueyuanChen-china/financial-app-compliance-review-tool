from __future__ import annotations

from collections import Counter

from compliance_review.domain.models import CoverageGateResult, RegressionComparison, Snapshot
from compliance_review.review.diff_review import DiffReviewPlan


def render_diff_report(
    snapshot: Snapshot,
    gate: CoverageGateResult,
    plan: DiffReviewPlan,
    regressions: RegressionComparison,
) -> str:
    """Render a compact Chinese operator report for a completed incremental run."""
    affected = [item for item in plan.impacts if item.affected]
    carried = set(plan.reuse.reused_unit_ids)
    terminal = set(plan.reuse.terminal_non_review_unit_ids)
    reviewed = set(snapshot.reviewed_rows)
    reviewed_partial = set(snapshot.reviewed_partial_rows)
    all_reviewed = reviewed | reviewed_partial
    counts = Counter(item.classification for item in regressions.changes)
    lines = [
        "# 合规增量审查报告",
        "",
        f"- 当前 Run：`{snapshot.run_id}`",
        f"- 直接基线：`{snapshot.baseline_run_id or '无'}`",
        "- 语义 Full 基线："
        f"`{snapshot.semantic_baseline_run_id or snapshot.baseline_run_id or '无'}`",
        f"- CI：`{snapshot.ci_status.upper()}`",
        f"- 覆盖完整性：`{'完整' if gate.complete else '不完整'}`",
        "",
        "## 增量摘要",
        "",
        f"- 变更文件：`{len(plan.diff.files)}`",
        f"- 影响判定：受影响 `{len(affected)}`，未受影响 `{len(plan.impacts) - len(affected)}`",
        f"- 实际重审 Unit：`{len(all_reviewed)}`",
        f"- 沿用前次结果 Unit：`{len(carried)}`",
        f"- 未派发的 Coverage Gap：`{len(set(plan.reuse.review_unit_ids) - all_reviewed)}`",
        "",
        "## 变更文件",
        "",
    ]
    lines.extend(
        f"- `{item.repo_id}:{item.path}`（{item.change_type}）" for item in plan.diff.files
    )
    if not plan.diff.files:
        lines.append("- 无代码变更。")
    lines.extend(["", "## 影响与执行", ""])
    for item in plan.impacts:
        if item.coverage_unit_id in reviewed:
            state = "已重审"
        elif item.coverage_unit_id in reviewed_partial:
            state = "已重审（证据未完整）"
        elif item.coverage_unit_id in carried:
            state = "沿用"
        elif item.coverage_unit_id in terminal:
            state = "无需执行（不要求/不适用）"
        elif item.affected:
            state = "需重审但未派发"
        else:
            state = "未受影响，但缺少可继承结论"
        lines.append(f"- `{item.coverage_unit_id}`：{state}；" + "；".join(item.reasons))
    lines.extend(["", "## 结果变化", ""])
    lines.extend(
        [
            f"- 新增风险：`{counts['regression']}`",
            f"- 已解决：`{counts['improvement']}`",
            f"- 延续或无变化：`{counts['unchanged']}`",
            f"- 警告：`{counts['warning']}`",
        ]
    )
    if gate.blocking_reasons:
        lines.extend(["", "## 阻断原因", ""])
        lines.extend(f"- {reason}" for reason in gate.blocking_reasons)
    lines.extend(["", "## 机器产物", ""])
    lines.extend(
        [
            f"- `runs/{snapshot.run_id}/diff/diff.json`",
            f"- `runs/{snapshot.run_id}/diff/impact-decisions.json`",
            f"- `runs/{snapshot.run_id}/coverage_manifest.json`",
            f"- `runs/{snapshot.run_id}/snapshot.json`",
        ]
    )
    return "\n".join(lines) + "\n"
