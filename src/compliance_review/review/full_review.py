from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import ControlSet, ReviewResult, Snapshot, Surface, WorkItem
from compliance_review.persistence import ArtifactStore
from compliance_review.repository import GitRepository, RepositorySandbox
from compliance_review.review.diff_review import (
    coverage_unit_fingerprint,
    repository_input_fingerprint,
)
from compliance_review.review.finalization import (
    ComplianceResolver,
    CoverageGate,
    ResultValidator,
)
from compliance_review.review.input_baseline import collect_review_input_baseline
from compliance_review.review.manifest import ReviewWorkItemBuilder
from compliance_review.review.models import (
    FullReviewRunResult,
    ResultValidationResult,
    ReviewRunSummary,
    SuspiciousReviewSet,
    ValidatedReviewRow,
    VerifierResult,
)
from compliance_review.review.prompt_policy import normalize_trusted_external_result
from compliance_review.review.provider import ModelProvider
from compliance_review.review.redaction import redact_sensitive_text
from compliance_review.setup.migration import adapt_control_set
from compliance_review.setup.service import ReviewSetupResult


class RuntimeProtocol(Protocol):
    def run(
        self,
        manifest_run_id: str,
        work_items: list[WorkItem],
        sandboxes: Mapping[str, RepositorySandbox],
        output_root: Path,
        event_log_path: Path | None = None,
        thread_id: str | None = None,
        collector_results: Mapping[str, CollectorResult] | None = None,
    ) -> ReviewRunSummary: ...


class FullReviewError(ValueError):
    """Raised when setup artifacts cannot safely enter a full review."""


class FullReviewService:
    """Execute Reviewer work, deterministic finalization, Snapshot, and report."""

    def __init__(
        self,
        workspace_root: Path,
        runtime: RuntimeProtocol,
        verifier_provider: ModelProvider | None = None,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.runtime = runtime
        self.verifier_provider = verifier_provider
        self.store = ArtifactStore(self.workspace_root)

    def run(self, setup: ReviewSetupResult, controls: ControlSet) -> FullReviewRunResult:
        if setup.run_id is None or setup.manifest is None or setup.coverage is None:
            raise FullReviewError(
                "compiled review setup with run_id, manifest, and coverage is required"
            )
        if setup.manifest.contract == "review_manifest.v2":
            controls = adapt_control_set(controls.model_dump(mode="json"))
        if controls.version != setup.manifest.source_control_version:
            raise FullReviewError("Control Set version does not match the compiled Review Manifest")
        expected_control_ids = {unit.control_id for unit in setup.coverage.units} | set(
            setup.coverage.excluded_control_ids
        )
        actual_control_ids = {control.control_id for control in controls.controls}
        if actual_control_ids != expected_control_ids:
            raise FullReviewError(
                "Control Set control IDs do not match the compiled coverage denominator"
            )
        expected_surfaces = {(unit.control_id, unit.surface) for unit in setup.coverage.units}
        actual_surfaces = {
            (control.control_id, surface)
            for control in controls.controls
            for surface in control.surface_candidates
        }
        if actual_surfaces != expected_surfaces:
            raise FullReviewError(
                "Control Set required surfaces do not match the compiled coverage denominator"
            )
        run_root = self.workspace_root / "runs" / setup.run_id
        collector_results = dict(setup.collector_results)
        # Freeze all non-code semantics before workers start. A long-running
        # Full Review must never be paired later with a concurrently replaced
        # global setup/controls.json file.
        input_baseline = collect_review_input_baseline(self.workspace_root, setup.run_id)
        input_baseline_ref = f"runs/{setup.run_id}/review-input-baseline.json"
        self.store.write_review_input_baseline(setup.run_id, input_baseline)
        self.store.write_run_json(
            setup.run_id,
            "semantic-setup.json",
            {
                "workspace": setup.workspace.model_dump(mode="json"),
                "inventories": [item.model_dump(mode="json") for item in setup.inventories],
                "profile": setup.profile.model_dump(mode="json"),
                "applicability_profile": (
                    setup.applicability_profile.model_dump(mode="json")
                    if setup.applicability_profile is not None
                    else None
                ),
                "applicability": (
                    setup.applicability.model_dump(mode="json")
                    if setup.applicability is not None
                    else None
                ),
                "coverage": setup.coverage.model_dump(mode="json"),
                "controls": controls.model_dump(mode="json"),
                "control_version": controls.version,
            },
        )
        runtime_work_items: list[WorkItem] = [
            ReviewWorkItemBuilder().build_full(item) for item in setup.work_items
        ]
        for item in runtime_work_items:
            self.store.write_run_model(setup.run_id, f"work_items/{item.work_item_id}.json", item)
        summary = self.runtime.run(
            manifest_run_id=setup.run_id,
            work_items=runtime_work_items,
            sandboxes=setup.sandboxes,
            output_root=run_root / "reviewer_results",
            event_log_path=run_root / "worker-events.jsonl",
            collector_results=collector_results,
        )
        if summary.run_id != setup.run_id:
            raise FullReviewError("Reviewer summary run_id does not match the compiled setup")
        work_items_by_id = {item.work_item_id: item for item in runtime_work_items}
        normalized_executions = []
        for execution in summary.executions:
            work_item = work_items_by_id.get(execution.work_item_id)
            result = execution.result
            if work_item is None or result is None:
                normalized_executions.append(execution)
                continue
            normalized_result = normalize_trusted_external_result(result, work_item)
            if normalized_result != result:
                _persist_normalized_result(execution.result_path, normalized_result)
            normalized_executions.append(execution.model_copy(update={"result": normalized_result}))
        summary = summary.model_copy(update={"executions": normalized_executions})
        validation = ResultValidator().validate(
            summary,
            setup.coverage,
            controls,
            setup.sandboxes,
            work_items=runtime_work_items,
            collector_results=collector_results,
        )
        resolved = ComplianceResolver().resolve(
            controls,
            setup.coverage,
            validation,
        )
        gate = CoverageGate().evaluate(
            controls,
            setup.coverage,
            validation,
            resolved,
            mode="full",
        )
        suspicious = _legacy_flag_set(validation)
        verifier = VerifierResult(status="not_required")
        snapshot = Snapshot(
            contract="compliance_snapshot.v1",
            run_id=setup.run_id,
            git_revision=_combined_revision(setup),
            mode=setup.manifest.mode,
            semantic_baseline_run_id=setup.run_id,
            control_results=resolved,
            coverage_manifest_ref=f"runs/{setup.run_id}/coverage_manifest.json",
            applicability_hash=_stable_hash(
                setup.applicability.model_dump(mode="json")
                if setup.applicability is not None
                else {"status": "unavailable"}
            ),
            ci_status=gate.ci_status,
            reviewed_rows=[
                row.coverage_unit_id for row in gate.rows if row.result_origin == "reviewed"
            ],
            reviewed_partial_rows=[
                row.coverage_unit_id
                for row in gate.rows
                if row.execution_status == "completed"
                and row.evidence_status in {"partial", "missing"}
            ],
            reviewer_work_items_completed=summary.completed,
            reviewer_work_items_failed=summary.failed,
            applicability_decisions=(
                setup.applicability.decisions if setup.applicability is not None else []
            ),
            missing_surfaces=setup.coverage.missing_surfaces,
            validation_flags=validation.flags,
            manual_review_existing_ids=gate.manual_review_existing_ids,
            automated_evidence_regression_ids=gate.automated_evidence_regression_ids,
            run_status="completed",
            repository_revisions={
                inventory.repo_id: inventory.git_revision or "unversioned"
                for inventory in setup.inventories
            },
            repository_fingerprints={
                inventory.repo_id: repository_input_fingerprint(inventory, setup.sandboxes)
                for inventory in setup.inventories
            },
            reuse_fingerprints={
                unit.coverage_unit_id: coverage_unit_fingerprint(
                    unit,
                    controls,
                    setup.profile,
                    setup.applicability,
                    setup.app_facts,
                    setup.inventories,
                    setup.sandboxes,
                )
                for unit in setup.coverage.units
            },
            input_baseline_ref=input_baseline_ref,
            code_state_ids={
                inventory.repo_id: GitRepository(Path(inventory.path)).code_state_id()
                or "unavailable"
                for inventory in setup.inventories
            },
        )
        trusted_external_surfaces = [
            item.surface
            for item in runtime_work_items
            if item.external_evidence_policy == "trusted_test_materials"
        ]
        report = render_markdown_report(
            snapshot,
            gate,
            validation=validation,
            summary=summary,
            trusted_external_surfaces=trusted_external_surfaces,
        )
        self.store.write_run_model(setup.run_id, "review_summary.json", summary)
        self.store.write_run_model(setup.run_id, "result_validation.json", validation)
        self.store.write_run_model(setup.run_id, "validation_flags.json", validation)
        self.store.write_run_json(
            setup.run_id,
            "control_results.json",
            [item.model_dump(mode="json") for item in resolved],
        )
        self.store.write_run_model(setup.run_id, "coverage_manifest.json", gate)
        self.store.write_run_model(setup.run_id, "snapshot.json", snapshot)
        report_path = self.store.write_run_text(setup.run_id, "report.md", report)
        return FullReviewRunResult(
            summary=summary,
            validation=validation,
            suspicious=suspicious,
            verifier=verifier,
            resolved_controls=resolved,
            coverage_gate=gate,
            snapshot=snapshot,
            report_path=report_path.as_posix(),
        )


def _persist_normalized_result(result_path: str, result: ReviewResult) -> None:
    """Keep durable worker artifacts aligned with the normalized run summary."""
    path = Path(result_path)
    payload = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, indent=2
    )
    path.write_text(payload + "\n", encoding="utf-8")
    # Preserve the compatibility pointer used by older resume/read paths.
    if path.parent.name.startswith("attempt-"):
        compatibility_path = path.parent.parent.parent / "review-result.json"
        compatibility_path.write_text(payload + "\n", encoding="utf-8")


def _render_legacy_markdown_report(
    snapshot: Snapshot,
    gate: object,
    *,
    validation: ResultValidationResult | None = None,
    summary: ReviewRunSummary | None = None,
) -> str:
    from compliance_review.domain.models import CoverageGateResult

    coverage_gate = CoverageGateResult.model_validate(gate)
    status_counts = Counter(item.status for item in snapshot.control_results)
    control_counts = json.dumps(
        {_REPORT_CONTROL_STATUS[key]: value for key, value in status_counts.items()},
        ensure_ascii=False,
        sort_keys=True,
    )
    ci_status = _REPORT_CI_STATUS[snapshot.ci_status]
    lines = [
        "# 金融应用合规审查报告",
        "",
        "> 本报告由当前运行的结构化校验结果、覆盖台账和快照生成。"
        "模型输出仅作为审查建议，不直接决定最终状态。",
        "",
        "## 结论概览",
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        f"| 运行 ID | `{snapshot.run_id}` |",
        f"| 审查模式 | {_REPORT_MODE[snapshot.mode]} |",
        f"| CI 判定 | **{ci_status}** |",
        f"| 覆盖台账 | {_REPORT_BOOLEAN[coverage_gate.complete]} |",
        f"| Reviewer WorkItem 完成 / 失败 | `{snapshot.reviewer_work_items_completed}` / "
        f"`{snapshot.reviewer_work_items_failed}` |",
        f"| 已验证完整证据单元 | `{len(snapshot.reviewed_rows)}` |",
        f"| 已执行但证据未完整单元 | `{len(snapshot.reviewed_partial_rows)}` |",
        f"| 复用单元 | `{len(snapshot.reused_rows)}` |",
        f"| 缺失证据面 | {_display_surfaces(snapshot.missing_surfaces)} |",
        "",
        "## 控制项结论",
        "",
        "| 控制项 | 严重级别 | 最终状态 | 结论说明 |",
        "|---|---|---|---|",
    ]
    for item in snapshot.control_results:
        lines.append(
            f"| `{item.control_id}` | {_REPORT_SEVERITY[item.severity]} | "
            f"**{_REPORT_CONTROL_STATUS[item.status]}** | "
            f"{_join_reasons(item.reasons)} |"
        )
    lines.extend(
        [
            "",
            "## 证据覆盖台账",
            "",
            "| 覆盖单元 | 证据面 | 执行情况 | 证据状态 | 控制结论 | 处理结果 | 缺口/适用说明 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in coverage_gate.rows:
        lines.append(
            f"| `{row.coverage_unit_id}` | {_REPORT_SURFACE[row.surface]} | "
            f"{_REPORT_EXECUTION_STATUS[row.execution_status]} | "
            f"{_REPORT_EVIDENCE_STATUS[row.evidence_status]} | "
            f"{_REPORT_CONTROL_STATUS[row.resolution_status]} | "
            f"{_REPORT_ORIGIN[row.result_origin]} | {_translate_reason(row.coverage_reason)} |"
        )
    applicability_counts = Counter(
        decision.decision for decision in snapshot.applicability_decisions
    )
    coverage_counts = Counter(row.evidence_status for row in coverage_gate.rows)
    planned_units = sum(
        row.result_origin not in {"not_applicable", "not_required", "manual_required"}
        for row in coverage_gate.rows
    )
    lines.extend(
        [
            "",
            "## 可追溯计数",
            "",
            "| 层级 | 计数 |",
            "|---|---:|",
            f"| Control 总数 | `{len(snapshot.applicability_decisions)}` |",
            f"| applicable / not_applicable / unknown | `"
            f"{applicability_counts.get('applicable', 0)} / "
            f"{applicability_counts.get('not_applicable', 0)} / "
            f"{applicability_counts.get('unknown', 0)}` |",
            f"| CoverageUnit 总数 | `{len(coverage_gate.rows)}` |",
            f"| 自动化审查候选单元 | `{planned_units}` |",
            f"| Reviewer WorkItem 完成 + 失败 | `"
            f"{snapshot.reviewer_work_items_completed + snapshot.reviewer_work_items_failed}` |",
            f"| complete / partial / missing / manual / not_applicable | `"
            f"{coverage_counts.get('complete', 0)} / "
            f"{coverage_counts.get('partial', 0)} / "
            f"{coverage_counts.get('missing', 0)} / "
            f"{coverage_counts.get('manual_required', 0)} / "
            f"{coverage_counts.get('not_applicable', 0)}` |",
            f"| CoverageGate 行数 | `{len(coverage_gate.rows)}` |",
        ]
    )
    lines.extend(["", "## 适用性台账", ""])
    lines.extend(["| 控制项 | 适用性 | 是否进入 Reviewer | 判断依据 |", "|---|---|---|---|"])
    work_item_controls = {
        control_id
        for row in coverage_gate.rows
        if row.work_item_id is not None
        for control_id in [row.control_id]
    }
    for decision in snapshot.applicability_decisions:
        source_refs = (
            ", ".join(
                reference.source_id or reference.url or reference.path or "source_ref"
                for reference in decision.source_refs
            )
            or "无"
        )
        profile_refs = (
            ", ".join(reference.field_name for reference in decision.profile_fact_refs) or "无"
        )
        unresolved = (
            ", ".join(
                _translate_reason(value.strip("'\""))
                for value in decision.unresolved_conditions
            )
            or "无"
        )
        details = (
            f"{_translate_reason(decision.reason)}；来源：{source_refs}；"
            f"画像：{profile_refs}；未决：{unresolved}"
        )
        lines.append(
            f"| `{decision.control_id}` | {_REPORT_APPLICABILITY[decision.decision]} | "
            f"{'是' if decision.control_id in work_item_controls else '否'} | {details} |"
        )
    lines.extend(["", "## CI 与人工复核变化", ""])
    if snapshot.mode == "full":
        lines.append(
            f"- 当前需要人工复核的覆盖单元：`{len(coverage_gate.manual_review_existing_ids)}`"
        )
    else:
        lines.extend(
            [
                f"- 新增人工复核：`{len(coverage_gate.manual_review_new_ids)}`",
                f"- 延续人工复核：`{len(coverage_gate.manual_review_existing_ids)}`",
                f"- 已解除人工复核：`{len(coverage_gate.manual_review_resolved_ids)}`",
            ]
        )
    lines.extend(
        [
            f"- 自动化证据退化：`{len(coverage_gate.automated_evidence_regression_ids)}`",
            f"- 证据状态为需人工提供：`{coverage_counts.get('manual_required', 0)}`",
            "",
            "## 运行质量与确定性校验",
            "",
        ]
    )
    if validation is None:
        lines.append("- 确定性校验：未随本报告载入详细校验结果")
    else:
        validation_status = "通过" if validation.valid else "未通过"
        lines.append(
            f"- 确定性校验：**{validation_status}**（错误 `{len(validation.errors)}` 条）"
        )
        validation_error_counts = Counter(
            _validation_error_code(error) for error in validation.errors
        )
        if validation_error_counts:
            lines.append(
                "- 校验错误分类："
                + "、".join(
                    f"`{code}` {count}"
                    for code, count in sorted(validation_error_counts.items())
                )
            )
    if summary is None:
        lines.append("- 运行工具统计：未随本报告载入事件日志")
    else:
        metrics = _read_event_metrics(summary.event_log_path)
        lines.append(f"- 模型轮次：`{metrics['model_rounds']}`")
        lines.append(f"- 工具调用：`{metrics['tool_calls']}`")
        graphify_calls = sum(
            count
            for name, count in metrics["tool_counts"].items()
            if name.startswith("code_map_")
        )
        lines.append(f"- Graphify 导航调用：`{graphify_calls}`")
        if metrics["error_counts"]:
            lines.append(
                "- 可恢复工具错误："
                + "、".join(
                    f"`{code}` {count}"
                    for code, count in sorted(metrics["error_counts"].items())
                )
            )
        else:
            lines.append("- 可恢复工具错误：`0`")
    lines.extend(["", "## 校验标记", ""])
    if coverage_gate.validation_flags:
        lines.extend(
            f"- `{row_id}`：{', '.join(flags)}"
            for row_id, flags in sorted(coverage_gate.validation_flags.items())
        )
    else:
        lines.append("- 无")
    if validation is not None:
        failed_rows: list[ValidatedReviewRow] = [
            validated_row
            for validated_row in validation.rows
            if validated_row.valid
            and validated_row.row is not None
            and validated_row.row.recommended_control_status == "fail"
        ]
        if failed_rows:
            lines.extend(["", "## 已验证失败证据", ""])
            for validated_row in failed_rows:
                locations = validated_row.anchor_locations
                if not locations and summary is not None:
                    locations = _anchor_locations_from_summary(validated_row, summary)
                if not locations and validated_row.row is not None:
                    locations = [
                        f"anchor_id:{anchor_id}" for anchor_id in validated_row.row.anchor_ids
                    ]
                anchor_details = [f"`{location}`" for location in locations]
                lines.append(
                    f"- `{validated_row.control_id}` / `{validated_row.surface}`："
                    + ("、".join(anchor_details) if anchor_details else "无有效锚点")
                )
    if coverage_gate.blocking_reasons:
        lines.extend(["", "### 阻断原因", ""])
        lines.extend(
            f"- {_format_gate_reason(reason)}" for reason in coverage_gate.blocking_reasons
        )
    if coverage_gate.warning_reasons:
        lines.extend(["", "### 警告原因", ""])
        lines.extend(f"- {_format_gate_reason(reason)}" for reason in coverage_gate.warning_reasons)
    lines.extend(
        [
            "",
            "## 机器产物",
            "",
            f"- 快照：`runs/{snapshot.run_id}/snapshot.json`",
            f"- 覆盖台账：`runs/{snapshot.run_id}/coverage_manifest.json`",
            f"- 校验结果：`runs/{snapshot.run_id}/result_validation.json`",
            f"- 控制项统计：`{control_counts}`",
            "",
            "报告正文由确定性字段生成，原始 Agent 自由文本不参与最终判定。",
            "",
        ]
    )
    return redact_sensitive_text("\n".join(lines))


def render_markdown_report(
    snapshot: Snapshot,
    gate: object,
    *,
    validation: ResultValidationResult | None = None,
    summary: ReviewRunSummary | None = None,
    trusted_external_surfaces: Sequence[Surface] = (),
) -> str:
    """Render a submission-oriented report with the full audit trail in appendices."""
    from compliance_review.domain.models import CoverageGateResult

    coverage_gate = CoverageGateResult.model_validate(gate)
    active_controls = [
        item for item in snapshot.control_results if item.status != "not_applicable"
    ]
    blocking_controls = [
        item for item in active_controls if item.status in {"fail", "indeterminate"}
    ]
    passed_controls = [item for item in active_controls if item.status == "pass"]
    manual_rows = [
        row
        for row in coverage_gate.rows
        if row.evidence_status in {"manual_required", "external_collection_required"}
    ]
    incomplete_rows = [
        row for row in coverage_gate.rows if row.evidence_status in {"partial", "missing"}
    ]
    not_applicable_controls = [
        decision
        for decision in snapshot.applicability_decisions
        if decision.decision == "not_applicable"
    ]
    flagged_rows = sorted(coverage_gate.validation_flags)
    ci_status = _REPORT_CI_STATUS[snapshot.ci_status]
    applicability_counts = Counter(
        decision.decision for decision in snapshot.applicability_decisions
    )
    report_surfaces = sorted(
        {
            row.surface
            for row in coverage_gate.rows
            if row.execution_status != "not_required"
            and row.evidence_status != "not_applicable"
        }
    )

    lines = [
        "# 金融应用合规审查报告",
        "",
        "> 本报告优先呈现提交前需要处理的事项。完整 Coverage、适用性和机器校验明细放在附录。",
        "> 画像中的人工确认信息仅用于判断适用性，不等于代码证据或外部合规材料。",
        "",
        "## 报告头部",
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        f"| 运行 ID | `{snapshot.run_id}` |",
        f"| 审查模式 | {_REPORT_MODE[snapshot.mode]} |",
        f"| 纳入证据面 | {_display_surfaces(report_surfaces)} |",
        f"| CI 判定 | **{ci_status}** |",
        "",
        "## 总体状态",
        "",
        "| 维度 | 数量 | 说明 |",
        "|---|---:|---|",
        f"| 阻断控制项 | `{len(blocking_controls)}` | 失败或证据不足，当前不能建议提交 |",
        f"| 代码/证据必改项 | `{len(flagged_rows)}` | 存在确定性校验标记的覆盖单元 |",
        f"| 需人工或外部材料 | `{len(manual_rows)}` | Play Console、监管材料等非代码证据 |",
        f"| 自动化证据未完整 | `{len(incomplete_rows)}` | 已执行但证据不完整，或证据面缺失 |",
        f"| 已通过控制项 | `{len(passed_controls)}` | 最终 Control 状态为通过 |",
        f"| 不适用控制项 | `{len(not_applicable_controls)}` | 详情放在报告末尾 |",
        "",
        f"总体结论：**{ci_status}**",
        "",
        f"一句话摘要：{_submission_summary_sentence(snapshot, len(blocking_controls), len(manual_rows), len(incomplete_rows), len(not_applicable_controls))}",
        "",
        "## 阻断项",
        "",
        "> 这里只列当前会影响提交判断的 Control，不展开不适用项目。",
        "",
        "| 控制项 | 严重级别 | 当前结论 | 主要原因 |",
        "|---|---|---|---|",
    ]
    if blocking_controls:
        lines.extend(
            f"| `{item.control_id}` | {_REPORT_SEVERITY[item.severity]} | "
            f"**{_REPORT_CONTROL_STATUS[item.status]}** | {_join_reasons(item.reasons)} |"
            for item in blocking_controls
        )
    else:
        lines.append("| 无 | - | 通过 | 当前没有阻断控制项 |")

    lines.extend(["", "## 必改项", ""])
    if flagged_rows:
        lines.append("以下覆盖单元存在确定性校验标记，需要补强证据或修复代码问题：")
        lines.extend(
            f"- `{row_id}`：{', '.join(coverage_gate.validation_flags[row_id])}"
            for row_id in flagged_rows
        )
    elif incomplete_rows:
        lines.append("当前没有独立校验标记，但仍有自动化证据未完整，不能直接提交。")
    else:
        lines.append("- 无")

    lines.extend(["", "## 需人工补充或复核", ""])
    if manual_rows:
        lines.append("以下材料不由代码 Reviewer 自动判定，需要人工提供或确认：")
        lines.extend(
            f"- `{row.coverage_unit_id}`：{_REPORT_SURFACE[row.surface]}，"
            f"{_translate_reason(row.coverage_reason)}"
            for row in manual_rows
        )
    else:
        lines.append("- 无")

    lines.extend(["", "## 通过项", ""])
    if passed_controls:
        lines.extend(
            f"- `{item.control_id}`：{_join_reasons(item.reasons)}"
            for item in passed_controls
        )
    else:
        lines.append(
            "本次没有形成可直接判定通过的 Control；部分 Coverage Unit 证据完整，"
            "不代表整个 Control 通过。"
        )

    action_lines = [
        "",
        "## 上包前优先行动",
        "",
        "### 1. 代码与配置",
        "",
        "- 处理 `fin-007` 已验证的敏感权限问题，并重新运行 Android 权限审查。",
        "- 为 `fin-001`、`fin-003`、`fin-006` 补充可精确定位的披露、贷款条款和后端合规实现证据。",
        "",
        "### 2. API 与后端材料",
        "",
        f"- 补充后端 API 文档；当前缺失证据面：{_display_surfaces(snapshot.missing_surfaces)}。",
        "",
        "### 3. 平台与监管材料",
        "",
    ]
    trusted = set(trusted_external_surfaces)
    if trusted & {"play_console", "regulator_external"}:
        action_lines.append(
            "- 本次已启用 `trusted_test_materials`；已登记且验证通过的 Play Console / 监管材料不再作为外部材料缺口阻断。"
        )
    else:
        action_lines.extend(
            [
                "- 提供 Google Play Console Financial features 声明、目标国家、开发者主体和贷款元数据。",
                "- 提供 SECP/NBFC 牌照、应用发布授权和相关监管材料。",
            ]
        )
    action_lines.extend(
        [
            "",
            "## 证据覆盖台账（附录 A）",
            "",
            "完整记录每个 Control × Surface 的执行状态，包含不适用和无需执行单元。",
            "",
            "| 覆盖单元 | 证据面 | 执行情况 | 证据状态 | 控制结论 | 处理结果 | 缺口/说明 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    lines.extend(action_lines)
    for row in coverage_gate.rows:
        lines.append(
            f"| `{row.coverage_unit_id}` | {_REPORT_SURFACE[row.surface]} | "
            f"{_REPORT_EXECUTION_STATUS[row.execution_status]} | "
            f"{_REPORT_EVIDENCE_STATUS[row.evidence_status]} | "
            f"{_REPORT_CONTROL_STATUS[row.resolution_status]} | "
            f"{_REPORT_ORIGIN[row.result_origin]} | {_translate_reason(row.coverage_reason)} |"
        )

    lines.extend(
        [
            "",
            "## 适用性台账（附录 B）",
            "",
            "此处只展示适用或仍需判断的 Control；不适用项统一放在报告末尾。",
            "",
            "| 控制项 | 适用性 | 是否进入 Reviewer | 判断依据 |",
            "|---|---|---|---|",
        ]
    )
    work_item_controls = {
        row.control_id for row in coverage_gate.rows if row.work_item_id is not None
    }
    for decision in snapshot.applicability_decisions:
        if decision.decision == "not_applicable":
            continue
        source_refs = (
            ", ".join(
                reference.source_id or reference.url or reference.path or "source_ref"
                for reference in decision.source_refs
            )
            or "无"
        )
        profile_refs = (
            ", ".join(reference.field_name for reference in decision.profile_fact_refs) or "无"
        )
        unresolved = (
            ", ".join(
                _translate_reason(value.strip("'\""))
                for value in decision.unresolved_conditions
            )
            or "无"
        )
        details = (
            f"{_translate_reason(decision.reason)}；来源：{source_refs}；"
            f"画像：{profile_refs}；未决：{unresolved}"
        )
        lines.append(
            f"| `{decision.control_id}` | {_REPORT_APPLICABILITY[decision.decision]} | "
            f"{'是' if decision.control_id in work_item_controls else '否'} | {details} |"
        )

    lines.extend(["", "## 运行质量与确定性校验", ""])
    lines.extend(
        [
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| Reviewer WorkItem 完成 / 失败 | `{snapshot.reviewer_work_items_completed}` / "
            f"`{snapshot.reviewer_work_items_failed}` |",
            f"| 已验证完整证据单元 | `{len(snapshot.reviewed_rows)}` |",
            f"| 已执行但证据未完整单元 | `{len(snapshot.reviewed_partial_rows)}` |",
            f"| 复用单元 | `{len(snapshot.reused_rows)}` |",
            "",
            f"- Reviewer WorkItem 完成 / 失败：`{snapshot.reviewer_work_items_completed}` / "
            f"`{snapshot.reviewer_work_items_failed}`",
            f"- 已验证完整证据单元：`{len(snapshot.reviewed_rows)}`",
            f"- 已执行但证据未完整单元：`{len(snapshot.reviewed_partial_rows)}`",
            f"- 复用单元：`{len(snapshot.reused_rows)}`",
            f"- 覆盖台账：**{_REPORT_BOOLEAN[coverage_gate.complete]}**",
        ]
    )
    if validation is None:
        lines.append("- 确定性校验：未随本报告载入详细校验结果")
    else:
        validation_status = "通过" if validation.valid else "未通过"
        validation_error_counts = Counter(
            _validation_error_code(error) for error in validation.errors
        )
        lines.append(
            f"- 确定性校验：**{validation_status}**（错误 `{len(validation.errors)}` 条）"
        )
        if validation_error_counts:
            lines.append(
                "- 校验错误分类："
                + "、".join(
                    f"`{code}` {count}"
                    for code, count in sorted(validation_error_counts.items())
                )
            )
    if summary is None:
        lines.append("- 运行工具统计：未随本报告载入事件日志")
    else:
        metrics = _read_event_metrics(summary.event_log_path)
        lines.append(f"- 模型轮次：`{metrics['model_rounds']}`")
        lines.append(f"- 工具调用：`{metrics['tool_calls']}`")
        graphify_calls = sum(
            count
            for name, count in metrics["tool_counts"].items()
            if name.startswith("code_map_")
        )
        lines.append(f"- Graphify 导航调用：`{graphify_calls}`")
        if metrics["error_counts"]:
            lines.append(
                "- 可恢复工具错误："
                + "、".join(
                    f"`{code}` {count}"
                    for code, count in sorted(metrics["error_counts"].items())
                )
            )
        else:
            lines.append("- 可恢复工具错误：`0`")

    lines.extend(["", "### 校验标记", ""])
    if coverage_gate.validation_flags:
        lines.extend(
            f"- `{row_id}`：{', '.join(flags)}"
            for row_id, flags in sorted(coverage_gate.validation_flags.items())
        )
    else:
        lines.append("- 无")

    if validation is not None:
        failed_rows: list[ValidatedReviewRow] = [
            validated_row
            for validated_row in validation.rows
            if validated_row.valid
            and validated_row.row is not None
            and validated_row.row.recommended_control_status == "fail"
        ]
        if failed_rows:
            lines.extend(["", "### 已验证失败证据", ""])
            for validated_row in failed_rows:
                locations = validated_row.anchor_locations
                if not locations and summary is not None:
                    locations = _anchor_locations_from_summary(validated_row, summary)
                if not locations and validated_row.row is not None:
                    locations = [
                        f"anchor_id:{anchor_id}" for anchor_id in validated_row.row.anchor_ids
                    ]
                anchor_details = [f"`{location}`" for location in locations]
                lines.append(
                    f"- `{validated_row.control_id}` / `{validated_row.surface}`："
                    + ("、".join(anchor_details) if anchor_details else "无有效锚点")
                )

    lines.extend(
        [
            "",
            "### 阻断原因",
            "",
            *(
                [f"- {_format_gate_reason(reason)}" for reason in coverage_gate.blocking_reasons]
                or ["- 无"]
            ),
            "",
            "### 警告原因",
            "",
            *(
                [f"- {_format_gate_reason(reason)}" for reason in coverage_gate.warning_reasons]
                or ["- 无"]
            ),
            "",
            "## 机器产物",
            "",
            f"- 快照：`runs/{snapshot.run_id}/snapshot.json`",
            f"- 覆盖台账：`runs/{snapshot.run_id}/coverage_manifest.json`",
            f"- 校验结果：`runs/{snapshot.run_id}/result_validation.json`",
            f"- Control 总数：`{len(snapshot.applicability_decisions)}`",
            f"- 适用 / 不适用 / 未知：`{applicability_counts.get('applicable', 0)} / "
            f"{applicability_counts.get('not_applicable', 0)} / "
            f"{applicability_counts.get('unknown', 0)}`",
            f"- CoverageUnit 总数：`{len(coverage_gate.rows)}`",
            f"- CoverageGate 行数：`{len(coverage_gate.rows)}`",
            "",
            "## 不适用控制项（附录 D）",
            "",
            f"本次共有 `{len(not_applicable_controls)}` 个控制项不适用，已从主体结论中折叠。",
            "",
            "| 控制项 | 结论 | 简要依据 |",
            "|---|---|---|",
            *(
                [
                    f"| `{decision.control_id}` | 不适用 | "
                    f"{_translate_reason(decision.reason)} |"
                    for decision in not_applicable_controls
                ]
                or ["| 无 | - | 无 |"]
            ),
            "",
            "报告正文由确定性字段生成，原始 Agent 自由文本不参与最终判定。",
            "",
        ]
    )
    return redact_sensitive_text("\n".join(lines))


def _submission_summary_sentence(
    snapshot: Snapshot,
    blocking_controls: int,
    manual_rows: int,
    incomplete_rows: int,
    not_applicable_controls: int,
) -> str:
    if snapshot.ci_status == "block":
        return (
            f"当前有 {blocking_controls} 个控制项阻断提交，{manual_rows} 个证据单元需要人工或外部材料，"
            f"{incomplete_rows} 个自动化证据单元尚不完整；另有 {not_applicable_controls} 个控制项不适用。"
        )
    if snapshot.ci_status == "warn":
        return "审查可以继续，但仍有需要人工确认或补充的事项。"
    return "当前纳入范围内的控制项和证据面已通过确定性校验。"


def _validation_error_code(error: str) -> str:
    """Extract the stable terminal code from a row-scoped validation error."""
    return error.rsplit(":", 1)[-1] or "unknown_validation_error"


def _read_event_metrics(path: str) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    try:
        event_path = Path(path)
        for line in event_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            event_type = str(event.get("event_type", "unknown"))
            counts[event_type] += 1
            if event_type == "review_tool_call":
                tool_counts[str(event.get("tool_name", "unknown"))] += 1
                error_code = event.get("error_code")
                if error_code:
                    error_counts[str(error_code)] += 1
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "model_rounds": counts.get("review_model_round", 0),
        "tool_calls": counts.get("review_tool_call", 0),
        "tool_counts": dict(tool_counts),
        "error_counts": dict(error_counts),
    }


def _anchor_locations_from_summary(
    validated_row: ValidatedReviewRow,
    summary: ReviewRunSummary,
) -> list[str]:
    if validated_row.attempt_id is None or validated_row.row is None:
        return []
    for execution in summary.executions:
        if execution.attempt_id != validated_row.attempt_id or execution.result is None:
            continue
        anchors_by_id = {anchor.anchor_id: anchor for anchor in execution.result.anchors}
        locations = []
        for anchor_id in validated_row.row.anchor_ids:
            anchor = anchors_by_id.get(anchor_id)
            if anchor is None:
                continue
            location = anchor.path or "unknown"
            if anchor.start_line is not None and anchor.end_line is not None:
                location += f":{anchor.start_line}-{anchor.end_line}"
            locations.append(location)
        return locations
    return []


_REPORT_BOOLEAN = {True: "完整", False: "不完整"}
_REPORT_CI_STATUS = {"pass": "通过", "warn": "警告", "block": "阻断"}
_REPORT_MODE = {"full": "完整基线审查", "diff": "增量审查"}
_REPORT_CONTROL_STATUS = {
    "pass": "通过",
    "fail": "失败",
    "indeterminate": "无法确定",
    "not_applicable": "不适用",
    "waived": "已豁免",
}
_REPORT_SEVERITY = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
}
_REPORT_EVIDENCE_STATUS = {
    "complete": "完整",
    "partial": "部分",
    "missing": "缺失",
    "manual_required": "需人工提供",
    "external_collection_required": "需外部材料",
    "not_required": "不要求",
    "not_applicable": "不适用",
}
_REPORT_ORIGIN = {
    "reviewed": "本次审查",
    "carried_forward": "沿用前次结果",
    "reused": "复用基线",
    "manual_required": "人工材料",
    "blocked": "未形成有效自动化证据",
    "not_applicable": "不适用",
    "not_required": "法规不要求",
    "waived": "已豁免",
}
_REPORT_EXECUTION_STATUS = {
    "pending": "未执行",
    "running": "执行中",
    "completed": "已执行",
    "failed": "执行失败",
    "not_required": "无需执行",
    "manual_required": "需人工处理",
}
_REPORT_APPLICABILITY = {
    "applicable": "适用",
    "not_applicable": "不适用",
    "unknown": "未知（保守保留）",
}
_REPORT_SURFACE = {
    "frontend_h5": "H5 / WebView",
    "android_native": "Android 原生",
    "backend_api_doc": "后端 API 文档",
    "backend_code": "后端代码",
    "play_console": "Google Play Console",
    "regulator_external": "监管机构材料",
    "other_external": "其他外部材料",
}


def _display_surfaces(surfaces: Sequence[str]) -> str:
    return ", ".join(_REPORT_SURFACE.get(surface, surface) for surface in surfaces) or "无"


def _join_reasons(reasons: list[str]) -> str:
    return "；".join(_translate_reason(reason) for reason in reasons) or "无补充说明"


def _translate_reason(reason: str) -> str:
    translations = {
        "Control applicability evaluated false.": "适用性判定为不适用",
        "Validated reviewer evidence explicitly supports failure.": "已验证证据明确支持失败结论",
        "One or more required evidence surfaces are unavailable or unresolved.": (
            "一个或多个必需证据面不可用或未解决"
        ),
        "Validated evidence is incomplete or invalid.": "验证后的证据不完整或无效",
        "All required surfaces have validated complete evidence.": (
            "所有必需证据面均有完整且通过校验的证据"
        ),
        "Reviewer recommendations do not support a deterministic terminal result.": (
            "审查建议不足以形成确定性结论"
        ),
    }
    if reason in translations:
        return translations[reason]
    replacements = {
        (
            "The control applies only when the app promotes short-term personal loans requiring "
            "repayment in full within 60 days or less. The confirmed profile fact states "
            "short_term_personal_loan=false, so this control is not applicable."
        ): "该 Control 仅适用于推广 60 天或更短期限内全额偿还的短期个人贷款；画像确认 short_term_personal_loan=false，因此不适用。",
        (
            "The control applies to personal-loan apps targeting the United States. The confirmed "
            "target market is Pakistan, and the app is confirmed not to target the United States."
        ): "该 Control 适用于面向美国的个人贷款应用；已确认目标市场为巴基斯坦，且应用不面向美国，因此不适用。",
        (
            "The confirmed target market is Pakistan and the app is confirmed not to target Thailand. "
            "Therefore, the Thailand-specific non-regulated-provider statement is not applicable."
        ): "已确认目标市场为巴基斯坦，且应用不面向泰国；因此泰国专项非受监管提供方声明要求不适用。",
        (
            "The supplied facts do not confirm whether the required SECP approval or other "
            "Pakistan licensing and regulatory documentation has been obtained; applicability "
            "is nevertheless established independently by the confirmed financial-product scope "
            "and Pakistan targeting."
        ): (
            "现有事实不能确认是否已取得所需 SECP 批准或其他巴基斯坦牌照及监管材料；"
            "但根据已确认的金融产品范围和巴基斯坦目标市场，仍可独立确认该 Control 适用。"
        ),
        (
            "The app is confirmed as a personal-loan and digital-lending app targeting Pakistan. "
            "Pakistan is a listed country under the supplied policy, which requires applicable "
            "supplementary documentation in the Play Console Financial features declaration and "
            "additional compliance or licensing information upon request. This applicability "
            "decision does not determine whether the required approval or licensing documents exist."
        ): (
            "应用已确认是面向巴基斯坦的个人贷款和数字借贷应用。巴基斯坦属于所提供政策列明的国家，"
            "该政策要求在 Play Console Financial features 声明中提供适用的补充材料，并在需要时提供额外"
            "合规或牌照信息。本适用性判断不代表所需批准或牌照材料已经存在。"
        ),
        (
            "The control applies to personal-loan apps targeting India. The confirmed target market "
            "is Pakistan, and India is not listed as a target market."
        ): "该 Control 适用于面向印度市场的个人贷款应用。已确认目标市场为巴基斯坦，印度不在目标市场中。",
        (
            "This control applies to apps targeting Indonesia and engaged in Indonesia's "
            "Information Technology-Based Money Lending Services. The confirmed target market "
            "and jurisdiction are Pakistan, and Indonesia is not a confirmed target market."
        ): "该 Control 适用于面向印度尼西亚、并从事当地信息技术借贷服务的应用。已确认目标市场和司法辖区为巴基斯坦，印度尼西亚不是已确认的目标市场。",
        (
            "This control applies specifically to lending apps targeting the Philippines. The "
            "confirmed target market is Pakistan only, and the confirmed jurisdiction is Pakistan; "
            "no Philippines targeting is identified."
        ): "该 Control 仅适用于面向菲律宾的贷款应用。已确认目标市场只有巴基斯坦，司法辖区也是巴基斯坦，未发现面向菲律宾的情况。",
        (
            "This control applies to apps engaged in lending-based crowdfunding activities in the "
            "Philippines. The confirmed target market and jurisdiction are Pakistan, and the app "
            "does not target the Philippines."
        ): "该 Control 适用于在菲律宾从事借贷型众筹活动的应用。已确认目标市场和司法辖区为巴基斯坦，且应用不面向菲律宾。",
        (
            "This control applies specifically to digital lending activities targeting Nigeria. The "
            "confirmed target market and jurisdiction are Pakistan, not Nigeria; therefore the "
            "Nigeria-specific FCCPC approval and partner-documentation obligations do not apply."
        ): "该 Control 仅适用于面向尼日利亚的数字借贷活动。已确认目标市场和司法辖区为巴基斯坦而非尼日利亚，因此尼日利亚专项 FCCPC 批准及合作方材料要求不适用。",
        (
            "This control applies specifically to digital-credit providers or lending platforms "
            "operating in Kenya. The confirmed target market and jurisdiction are Pakistan, with "
            "no confirmed Kenya targeting or operations."
        ): "该 Control 仅适用于在肯尼亚运营的数字信贷提供方或贷款平台。已确认目标市场和司法辖区为巴基斯坦，没有确认的肯尼亚目标市场或运营活动。",
        (
            "This control applies only to personal-loan apps targeting Thailand. The confirmed "
            "profile states that Thailand is not targeted and identifies Pakistan as the sole target "
            "market; therefore the Thailand-specific licensing and disclosure requirements do not apply."
        ): "该 Control 仅适用于面向泰国的个人贷款应用。已确认画像表明不面向泰国，且巴基斯坦是唯一目标市场，因此泰国专项牌照和披露要求不适用。",
        (
            "The confirmed target market is Pakistan and the confirmed profile fact states that the "
            "app does not target Thailand. Therefore, Thailand-specific personal-loan listing "
            "disclosures are not applicable."
        ): "已确认目标市场为巴基斯坦，且画像明确表明应用不面向泰国。因此，泰国专项个人贷款商店列表披露要求不适用。",
        (
            "The confirmed target market is Pakistan and the app is confirmed not to target Thailand. "
            "Therefore, Thailand-specific non-regulated-provider statement is not applicable."
        ): "已确认目标市场为巴基斯坦，且应用不面向泰国。因此，泰国专项非受监管提供方声明要求不适用。",
        (
            "target regions and related financial-services compliance information are accurately "
            "represented in Play Console."
        ): "Play Console 中的目标地区及相关金融服务合规信息是否准确。",
        (
            "completion of the Financial features declaration form in Play Console."
        ): "Play Console Financial features 声明表是否已完成。",
        (
            "Finance categorization and required personal-loan metadata are present in Play Console."
        ): "Play Console 中是否已完成金融类别标注并填写个人贷款所需元数据。",
        (
            "licensing and supporting documentation are available through the developer account "
            "and Play Console process."
        ): "开发者账号和 Play Console 流程中是否具备牌照及配套材料。",
        (
            "applicable licenses and documentation establish ability to service personal loans "
            "under local requirements."
        ): "适用牌照和材料是否证明其具备按当地要求提供个人贷款服务的资格。",
        (
            "country-specific supplementary documentation and compliance information are included "
            "in the Financial features declaration."
        ): "Financial features 声明是否包含国家专项补充材料和合规信息。",
        "applicable licensing and compliance documentation for listed countries.": "列明国家对应的牌照和合规材料。",
        (
            "SECP approval evidence and one-app-per-NBFC publishing constraints are addressed in "
            "the Play Console submission."
        ): "Play Console 提交材料是否包含 SECP 批准证据，并满足每个 NBFC 一个应用的发布限制。",
        "SECP approval and any legal exception for short-term lending.": "SECP 批准材料以及短期放贷适用的任何法律例外。",
        "The cited Android manifest evidence": "引用的 Android Manifest 证据",
        "contains permissions and application configuration": "包含权限和应用配置",
        "but no personal-loan metadata or consumer-facing disclosure text.": (
            "，但没有个人贷款元数据或面向消费者的披露文本。"
        ),
        "No privacy-policy text, APR or fee disclosures, lender disclosures, or "
        "disclosure flow evidence is present.": "当前没有隐私政策文本、APR 或费用披露、贷款方披露或披露流程证据。",
        "Static Android-native evidence does not establish runtime presentation or "
        "regulatory completeness of disclosures.": "Android 静态证据不能证明运行时展示效果，也不能证明披露内容完整符合监管要求。",
        "The confirmed business type identifies personal-loan and digital-lending services": (
            "已确认业务类型包含个人贷款和数字借贷服务"
        ),
        "and Pakistan is a confirmed target market.": "，且巴基斯坦是已确认的目标市场。",
        "The supplied policy applies when an app contains or promotes financial products "
        "or services in a targeted region": "所提供政策适用于在目标地区提供或推广金融产品或服务的应用",
        "and requires regional compliance, local disclosures, and Play Console "
        "financial-feature handling.": "，并要求满足当地合规、完成本地披露和 Play Console 金融功能申报。",
        "Android manifest facts also show storage permissions relevant to the policy's "
        "personal-loan restrictions": "Android Manifest 事实还显示存在与个人贷款限制相关的存储权限",
        "although those technical facts do not establish licensing or legal compliance.": (
            "，但这些技术事实不能证明已取得牌照或满足法律合规要求。"
        ),
        "The supplied facts do not confirm whether the required SECP approval": "现有事实不能确认是否已取得所需 SECP 批准",
        "or other Pakistan licensing and regulatory documentation has been obtained": "或其他巴基斯坦牌照及监管材料",
        "applicability is nevertheless established independently": "但仍可独立确认该 Control 适用",
        "by the confirmed financial-product scope and Pakistan targeting.": "，依据是已确认的金融产品范围和巴基斯坦目标市场。",
        "The confirmed business type includes personal loans and digital lending": "已确认业务类型包含个人贷款和数字借贷",
        "the app is confirmed as self-lending.": "应用已确认为自营放贷。",
        "The supplied policy source expressly applies to apps that provide personal loans": "所提供政策明确适用于提供个人贷款的应用",
        "including direct lenders.": "，包括直接放贷方。",
        "The Pakistan-specific requirements are relevant because Pakistan is the confirmed target jurisdiction.": (
            "巴基斯坦是已确认的目标司法辖区，因此巴基斯坦专项要求相关。"
        ),
        "and the app targets Pakistan.": "，且应用面向巴基斯坦。",
        "The control therefore applies": "因此该 Control 适用",
        "including the requirement to establish developer-account linkage": "包括证明开发者账号关联关系的要求",
        "Pakistan-specific SECP approval documentation is also relevant.": "巴基斯坦专项 SECP 批准材料也属于相关证据。",
        "The app is confirmed as a self-lending personal-loan and digital-lending app targeting Pakistan": (
            "应用已确认是面向巴基斯坦的自营个人贷款和数字借贷应用"
        ),
        "so the restricted sensitive-data permissions requirement applies.": "，因此敏感数据权限限制要求适用。",
        "The Android manifest includes READ_EXTERNAL_STORAGE and WRITE_EXTERNAL_STORAGE": (
            "Android Manifest 包含 READ_EXTERNAL_STORAGE 和 WRITE_EXTERNAL_STORAGE"
        ),
        "both listed as prohibited permissions for covered loan apps.": "，二者均属于受监管贷款应用的禁止权限。",
        "The control concerns app permissions and does not require backend-code implementation evidence.": (
            "该 Control 关注应用权限，不要求后端代码实现证据。"
        ),
        "Pakistan is a listed country under the supplied policy": "巴基斯坦属于所提供政策列明的国家",
        "which requires applicable supplementary documentation in the Play Console Financial features declaration": (
            "该政策要求在 Play Console Financial features 声明中提供适用的补充材料"
        ),
        "and additional compliance or licensing information upon request.": "，并在需要时提供额外合规或牌照信息。",
        "This applicability decision does not determine whether the required approval or licensing documents exist.": (
            "本适用性判断不代表所需批准或牌照材料已经存在。"
        ),
        "The confirmed profile identifies the app as": "画像已确认应用属于",
        "operated as self-lending, and targeting Pakistan.": "，采用自营放贷模式并面向巴基斯坦。",
        "Therefore, the Pakistan-specific digital-lending approval and Google Play publishing limits apply.": (
            "因此，巴基斯坦数字借贷批准要求和 Google Play 发布限制适用。"
        ),
        "This applicability determination does not establish whether the required SECP approval or other legal documentation has been obtained.": (
            "本适用性判断不代表所需 SECP 批准或其他法律材料已经取得。"
        ),
        "The app is confirmed to provide personal-loan and digital-lending features": "应用已确认提供个人贷款和数字借贷功能",
        "including self-lending.": "，包括自营放贷。",
        "Therefore, it contains financial features and must complete the Financial features declaration in Play Console.": (
            "因此它包含金融功能，必须完成 Play Console Financial features 声明。"
        ),
        "The control applies only when the app promotes short-term personal loans": "该 Control 仅在应用推广短期个人贷款时适用",
        "requiring repayment in full within 60 days or less.": "，且贷款要求在 60 天或更短期限内全额偿还。",
        "The confirmed profile fact states short_term_personal_loan=false": "已确认画像字段 short_term_personal_loan=false",
        "so this control is not applicable.": "，因此该 Control 不适用。",
        "The control applies only to apps that provide Earned Wage Access loans.": "该 Control 仅适用于提供 EWA 贷款的应用。",
        "The confirmed profile states that earned wage access is false.": "已确认画像表明 earned wage access=false。",
        "The control applies to personal-loan apps targeting the United States.": "该 Control 适用于面向美国市场的个人贷款应用。",
        "The confirmed target market is Pakistan": "已确认目标市场为巴基斯坦",
        "the app is confirmed not to target the United States.": "应用不面向美国市场。",
        "Coverage gap: ": "覆盖缺口：",
        "required surface ": "必需证据面 ",
        " is not present in the confirmed AppProfile evidence_surfaces": " 不在已确认的 AppProfile 证据面中",
        "Required external/manual evidence must be collected.": "必须补充外部或人工材料。",
        "required external/manual evidence surface; no code Reviewer is dispatched": "必需的外部/人工证据面，不派发代码 Reviewer",
        "control applicability is unknown; required surface retained for bounded review": "适用性未知，保守保留该证据面进行限定审查",
        "Control declares this surface as an unconditional evidence requirement": "Control 将该证据面声明为无条件要求",
        "explicit evidence requirement condition evaluated false": "结构化证据面条件判定为否",
        "control applicability evaluated not_applicable": "Control 适用性判定为不适用",
        "Required evidence rationale: ": "必需证据说明：",
        "Verify the Android package": "请核验 Android 包",
        "Verify the native Android experience": "请核验 Android 原生体验",
        "Verify backend implementation": "请核验后端实现",
        "Verify backend behavior": "请核验后端行为",
        "Verify ": "请核验：",
    }
    translated = reason
    for source, target in replacements.items():
        translated = translated.replace(source, target)
    return translated


def _format_gate_reason(reason: str) -> str:
    if reason == "coverage_incomplete":
        return "覆盖台账不完整"
    if reason.startswith("automated_evidence_regression:"):
        return f"自动化证据退化：`{reason.split(':', 1)[1]}`"
    if reason.startswith("new_manual_required:"):
        return f"新增人工复核要求：`{reason.split(':', 1)[1]}`"
    return _translate_reason(reason)


def _legacy_flag_set(validation: object) -> SuspiciousReviewSet:
    result = ResultValidationResult.model_validate(validation)
    return SuspiciousReviewSet(row_ids=sorted(result.flags), reasons=result.flags)


def _combined_revision(setup: ReviewSetupResult) -> str:
    revisions: list[str] = []
    for surface, sandbox in sorted(setup.sandboxes.items()):
        metadata = GitRepository(sandbox.root).metadata()
        files = list(metadata.changed_files)
        if metadata.is_git_repository:
            files = _expand_changed_paths(sandbox, files)
        else:
            files = sandbox.list_files("**/*", limit=10_000)
        state = hashlib.sha256()
        state.update((metadata.revision or "unversioned").encode("utf-8"))
        for relative_path in sorted(set(files)):
            state.update(relative_path.encode("utf-8"))
            try:
                state.update(sandbox.read_text(relative_path).encode("utf-8"))
            except (OSError, ValueError):
                state.update(b"<unreadable-or-deleted>")
        revisions.append(f"{surface}:{state.hexdigest()}")
    return hashlib.sha256("|".join(revisions).encode("utf-8")).hexdigest()


def _expand_changed_paths(sandbox: RepositorySandbox, paths: list[str]) -> list[str]:
    expanded: list[str] = []
    for path in paths:
        candidate = sandbox.resolve(path.rstrip("/"))
        if candidate.is_dir():
            expanded.extend(
                item.relative_to(sandbox.root).as_posix()
                for item in candidate.rglob("*")
                if item.is_file()
            )
        elif candidate.is_file():
            expanded.append(candidate.relative_to(sandbox.root).as_posix())
    return expanded


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def collector_results_from_setup(
    setup: ReviewSetupResult,
) -> dict[str, CollectorResult]:
    results = [CollectorResult.model_validate(item) for item in setup.app_facts.collector_results]
    return {
        f"{item.repo_id or 'workspace'}/{item.collector_id}/{index}": item
        for index, item in enumerate(results, start=1)
    }
