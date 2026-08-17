from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import ControlSet, Snapshot, WorkItem
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
from compliance_review.review.models import (
    FullReviewRunResult,
    ResultValidationResult,
    ReviewRunSummary,
    SuspiciousReviewSet,
    VerifierResult,
)
from compliance_review.review.provider import ModelProvider
from compliance_review.review.redaction import redact_sensitive_text
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
            for surface in control.required_surfaces
        }
        if actual_surfaces != expected_surfaces:
            raise FullReviewError(
                "Control Set required surfaces do not match the compiled coverage denominator"
            )
        run_root = self.workspace_root / "runs" / setup.run_id
        collector_results = dict(setup.collector_results)
        summary = self.runtime.run(
            manifest_run_id=setup.run_id,
            work_items=setup.work_items,
            sandboxes=setup.sandboxes,
            output_root=run_root / "reviewer_results",
            event_log_path=run_root / "worker-events.jsonl",
            collector_results=collector_results,
        )
        if summary.run_id != setup.run_id:
            raise FullReviewError("Reviewer summary run_id does not match the compiled setup")
        validation = ResultValidator().validate(
            summary,
            setup.coverage,
            controls,
            setup.sandboxes,
            work_items=setup.work_items,
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
                and row.evidence_status == "partial"
                and row.result_origin == "blocked"
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
        )
        report = render_markdown_report(snapshot, gate)
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


def render_markdown_report(snapshot: Snapshot, gate: object) -> str:
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
        unresolved = ", ".join(decision.unresolved_conditions) or "无"
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
            "",
            "## 校验标记",
            "",
        ]
    )
    if coverage_gate.validation_flags:
        lines.extend(
            f"- `{row_id}`：{', '.join(flags)}"
            for row_id, flags in sorted(coverage_gate.validation_flags.items())
        )
    else:
        lines.append("- 无")
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
}
_REPORT_ORIGIN = {
    "reviewed": "本次审查",
    "reused": "复用基线",
    "manual_required": "人工材料",
    "blocked": "未形成有效自动化证据",
    "not_applicable": "不适用",
    "waived": "已豁免",
}
_REPORT_EXECUTION_STATUS = {
    "pending": "未执行",
    "running": "执行中",
    "completed": "已执行",
    "failed": "未执行",
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
    return translations.get(reason, reason)


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
