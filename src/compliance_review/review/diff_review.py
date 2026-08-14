from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence

from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import (
    ApplicabilitySet,
    ContractModel,
    ControlSet,
    CoverageImpact,
    CoverageSet,
    CoverageUnit,
    DiffFile,
    DiffResult,
    RegressionChange,
    RegressionComparison,
    RepositoryDiff,
    ReuseDecision,
    ReusePlan,
    Snapshot,
    Surface,
    WorkItem,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.repository import GitRepository, RepositorySandbox
from compliance_review.review.finalization import (
    ComplianceResolver,
    CoverageGate,
    ResultValidator,
    is_automatable_surface,
)
from compliance_review.review.models import (
    DiffReviewRunResult,
    ResultValidationResult,
    ReviewRunSummary,
    SuspiciousReviewSet,
    ValidatedReviewRow,
    VerifierResult,
)
from compliance_review.review.provider import ModelProvider
from compliance_review.setup.models import AppFactSet, AppProfile, RepositoryInventory
from compliance_review.setup.service import ReviewSetupResult


class DiffReviewError(ValueError):
    """Raised when a diff review cannot fail closed."""


class DiffRuntimeProtocol(Protocol):
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


class DiffReviewPlan(ContractModel):
    contract: Literal["diff_review_plan.v1"] = "diff_review_plan.v1"
    diff: DiffResult
    impacts: list[CoverageImpact]
    reuse: ReusePlan
    fingerprints: dict[str, str]
    review_work_item_ids: list[str]


class DiffReviewPlanner:
    """Build deterministic impact and reuse decisions per CoverageUnit."""

    def plan(
        self,
        setup: ReviewSetupResult,
        controls: ControlSet,
        previous_snapshot: Snapshot | None,
        previous_validation: ResultValidationResult | None = None,
        baseline_run_id: str | None = None,
    ) -> DiffReviewPlan:
        previous_revisions = previous_snapshot.repository_revisions if previous_snapshot else {}
        repository_diffs: list[RepositoryDiff] = []
        all_files: list[DiffFile] = []
        errors: list[str] = []
        unmapped: list[str] = []
        for inventory in setup.inventories:
            diff = GitRepository(Path(inventory.path)).diff(
                inventory.repo_id,
                previous_revisions.get(inventory.repo_id),
                inventory.git_revision,
            )
            surface = inventory.detected_surface or inventory.declared_surface
            if surface is None:
                unmapped.append(inventory.repo_id)
            if surface is not None:
                diff = diff.model_copy(
                    update={
                        "files": [
                            item.model_copy(update={"surface": surface}) for item in diff.files
                        ]
                    }
                )
            repository_diffs.append(diff)
            all_files.extend(diff.files)
            if not diff.comparable:
                errors.append(f"{inventory.repo_id}:{diff.error_code or 'not_comparable'}")
        diff_result = DiffResult(
            baseline_run_id=baseline_run_id,
            repositories=repository_diffs,
            files=all_files,
            comparable=not errors and not unmapped,
            errors=errors,
            unmapped_repo_ids=unmapped,
        )
        fingerprints = (
            {
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
            }
            if setup.coverage is not None
            else {}
        )
        impacts = [
            CoverageImpact(
                coverage_unit_id=unit.coverage_unit_id,
                affected=_unit_affected(unit.surface, all_files, diff_result, setup.inventories),
                reasons=_impact_reasons(unit.surface, all_files, diff_result, setup.inventories),
                repository_ids=_repository_ids_for_surface(unit.surface, setup.inventories),
            )
            for unit in (setup.coverage.units if setup.coverage else [])
        ]
        impact_by_id = {item.coverage_unit_id: item for item in impacts}
        previous_rows = {
            row.row_id: row
            for row in (previous_validation.rows if previous_validation else [])
            if row.valid and row.row is not None and row.result_origin == "reviewed"
        }
        decisions: list[ReuseDecision] = []
        reused: list[str] = []
        review_units: set[str] = set()
        terminal: list[str] = []
        work_item_by_unit = {
            unit_id: work_item
            for work_item in setup.work_items
            for unit_id in work_item.coverage_unit_ids
        }
        for unit in setup.coverage.units if setup.coverage else []:
            current_fp = fingerprints[unit.coverage_unit_id]
            impact = impact_by_id[unit.coverage_unit_id]
            previous_fp = (
                previous_snapshot.reuse_fingerprints.get(unit.coverage_unit_id)
                if previous_snapshot
                else None
            )
            previous_row = previous_rows.get(f"{unit.control_id}:{unit.surface}")
            reusable = unit.coverage_status == "not_applicable" or (
                not impact.affected
                and previous_snapshot is not None
                and previous_snapshot.run_status == "completed"
                and previous_fp == current_fp
                and previous_row is not None
                and previous_row.row is not None
                and previous_row.row.recommended_control_status == "pass"
                and previous_row.row.evidence_status == "complete"
            )
            reasons = list(impact.reasons)
            if unit.coverage_status == "not_applicable":
                reasons.append("coverage unit is not applicable")
                terminal.append(unit.coverage_unit_id)
            elif reusable:
                reasons.append("exact fingerprint and valid terminal PASS are available")
                reused.append(unit.coverage_unit_id)
            else:
                if previous_snapshot is None:
                    reasons.append("no previous snapshot")
                elif previous_fp != current_fp:
                    reasons.append("reuse fingerprint changed")
                elif previous_row is None:
                    reasons.append("previous valid review row is missing")
                review_units.add(unit.coverage_unit_id)
            decisions.append(
                ReuseDecision(
                    coverage_unit_id=unit.coverage_unit_id,
                    reusable=reusable,
                    current_fingerprint=current_fp,
                    previous_fingerprint=previous_fp,
                    previous_run_id=previous_snapshot.run_id if previous_snapshot else None,
                    reasons=reasons,
                )
            )
        selected_work_items = {
            work_item_by_unit[unit_id].work_item_id
            for unit_id in review_units
            if unit_id in work_item_by_unit
        }
        # A WorkItem is atomic. If one unit in it needs review, all its units are reviewed.
        for work_item in setup.work_items:
            if work_item.work_item_id in selected_work_items:
                review_units.update(work_item.coverage_unit_ids)
                reused[:] = [
                    unit_id for unit_id in reused if unit_id not in work_item.coverage_unit_ids
                ]
        reuse_plan = ReusePlan(
            baseline_run_id=previous_snapshot.run_id if previous_snapshot else baseline_run_id,
            review_unit_ids=sorted(review_units),
            reused_unit_ids=sorted(reused),
            terminal_non_review_unit_ids=sorted(terminal),
            decisions=decisions,
            complete=(
                set(review_units) | set(reused) | set(terminal)
                == {
                    unit.coverage_unit_id
                    for unit in (setup.coverage.units if setup.coverage else [])
                }
            ),
        )
        return DiffReviewPlan(
            diff=diff_result,
            impacts=impacts,
            reuse=reuse_plan,
            fingerprints=fingerprints,
            review_work_item_ids=sorted(selected_work_items),
        )


def coverage_unit_fingerprint(
    unit: CoverageUnit,
    controls: ControlSet,
    profile: AppProfile,
    applicability: ApplicabilitySet | None,
    app_facts: AppFactSet,
    inventories: Sequence[RepositoryInventory],
    sandboxes: Mapping[str, RepositorySandbox],
) -> str:
    coverage_unit_id = unit.coverage_unit_id
    control = next(item for item in controls.controls if item.control_id == unit.control_id)
    repositories = [
        {
            "repo_id": item.repo_id,
            "surface": item.detected_surface or item.declared_surface,
            "revision": item.git_revision,
            "input_fingerprint": repository_input_fingerprint(item, sandboxes),
        }
        for item in sorted(inventories, key=lambda value: value.repo_id)
        if (item.detected_surface or item.declared_surface) == unit.surface
    ]
    payload = {
        "coverage_unit_id": coverage_unit_id,
        "control": control.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
        "applicability": applicability.model_dump(mode="json") if applicability else None,
        "required_surface": unit.surface,
        "required_evidence_strength": unit.required_evidence_strength,
        "repositories": repositories,
        "collector_facts": [
            item.model_dump(mode="json")
            for item in app_facts.facts
            if item.source_surface == unit.surface
        ],
        "reuse_invalidation_keys": sorted(control.reuse_invalidation_keys),
    }
    return stable_hash(payload)


def repository_input_fingerprint(
    inventory: RepositoryInventory,
    sandboxes: Mapping[str, RepositorySandbox],
) -> str:
    del sandboxes
    try:
        sandbox = RepositorySandbox(Path(inventory.path))
    except ValueError:
        return stable_hash({"repo_id": inventory.repo_id, "missing": True})
    digest = hashlib.sha256()
    for relative in sandbox.list_files("**/*", limit=100_000):
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(sandbox.read_text(relative).encode("utf-8"))
        except (OSError, ValueError):
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _repository_ids_for_surface(
    surface: Surface, inventories: Sequence[RepositoryInventory]
) -> list[str]:
    return sorted(
        item.repo_id
        for item in inventories
        if (item.detected_surface or item.declared_surface) == surface
    )


def _unit_affected(
    surface: Surface,
    files: Sequence[DiffFile],
    diff: DiffResult,
    inventories: Sequence[RepositoryInventory],
) -> bool:
    repos = set(_repository_ids_for_surface(surface, inventories))
    if not diff.comparable:
        return bool(repos)
    return any(item.repo_id in repos for item in files)


def _impact_reasons(
    surface: Surface,
    files: Sequence[DiffFile],
    diff: DiffResult,
    inventories: Sequence[RepositoryInventory],
) -> list[str]:
    repos = set(_repository_ids_for_surface(surface, inventories))
    if not diff.comparable:
        return (
            ["repository diff is not comparable; fail closed"]
            if repos
            else ["surface has no mapped repository"]
        )
    changed = sorted(item.path for item in files if item.repo_id in repos)
    return (
        [f"changed files: {', '.join(changed)}"]
        if changed
        else ["no mapped repository file changed"]
    )


def compare_regression(
    current_snapshot: Snapshot,
    previous_snapshot: Snapshot | None,
    missing_evidence_policy: Mapping[str, Literal["warn", "block"]],
) -> RegressionComparison:
    previous = (
        {item.control_id: item.status for item in previous_snapshot.control_results}
        if previous_snapshot
        else {}
    )
    changes: list[RegressionChange] = []
    for item in current_snapshot.control_results:
        old = previous.get(item.control_id)
        classification: Literal["regression", "warning", "improvement", "unchanged"]
        if old == "pass" and item.status == "fail":
            classification = "regression"
        elif old == "pass" and item.status == "indeterminate":
            classification = (
                "warning"
                if missing_evidence_policy.get(item.control_id) == "warn"
                else "regression"
            )
        elif old == "fail" and item.status == "pass":
            classification = "improvement"
        else:
            classification = "unchanged"
        changes.append(
            RegressionChange(
                coverage_unit_id=(
                    item.coverage_unit_ids[0] if item.coverage_unit_ids else item.control_id
                ),
                control_id=item.control_id,
                previous_status=old,
                current_status=item.status,
                classification=classification,
                reason=f"{old or 'absent'} -> {item.status}",
            )
        )
    ci_status: Literal["pass", "warn", "block"] = "pass"
    if any(item.classification == "regression" for item in changes):
        ci_status = "block"
    elif any(item.classification == "warning" for item in changes):
        ci_status = "warn"
    return RegressionComparison(
        baseline_run_id=previous_snapshot.run_id if previous_snapshot else None,
        current_run_id=current_snapshot.run_id,
        changes=changes,
        ci_status=ci_status,
    )


class DiffReviewService:
    """Layer deterministic diff/reuse planning over the existing review finalization flow."""

    def __init__(
        self,
        workspace_root: Path,
        runtime: DiffRuntimeProtocol,
        verifier_provider: ModelProvider | None = None,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.runtime = runtime
        self.verifier_provider = verifier_provider
        self.store = ArtifactStore(self.workspace_root)

    def run(
        self,
        setup: ReviewSetupResult,
        controls: ControlSet,
        previous_snapshot: Snapshot,
        previous_validation: ResultValidationResult | None = None,
    ) -> DiffReviewRunResult:
        if setup.run_id is None or setup.coverage is None or setup.manifest is None:
            raise DiffReviewError("compiled setup with run_id, coverage, and manifest is required")
        if setup.manifest.mode != "diff":
            raise DiffReviewError("DiffReviewService requires a diff-mode review manifest")
        if controls.version != setup.manifest.source_control_version:
            raise DiffReviewError("Control Set version does not match compiled review manifest")
        if previous_validation is None:
            try:
                previous_validation = ResultValidationResult.model_validate(
                    self.store.read_run_json(previous_snapshot.run_id, "result_validation.json")
                )
            except (OSError, ValueError, TypeError) as exc:
                raise DiffReviewError(
                    "previous validated rows are required for safe reuse"
                ) from exc
        plan = DiffReviewPlanner().plan(
            setup,
            controls,
            previous_snapshot,
            previous_validation,
            baseline_run_id=previous_snapshot.run_id,
        )
        if not plan.reuse.complete:
            raise DiffReviewError("diff plan does not cover every current CoverageUnit")
        run_root = self.workspace_root / "runs" / setup.run_id
        self.store.write_run_model(setup.run_id, "diff.json", plan.diff)
        self.store.write_run_json(
            setup.run_id, "impact.json", [item.model_dump(mode="json") for item in plan.impacts]
        )
        self.store.write_run_model(setup.run_id, "reuse-plan.json", plan.reuse)
        review_units = set(plan.reuse.review_unit_ids)
        review_coverage = setup.coverage.model_copy(
            update={
                "units": [
                    unit for unit in setup.coverage.units if unit.coverage_unit_id in review_units
                ]
            }
        )
        review_work_items = _filtered_work_items(setup.work_items, review_units)
        review_sandboxes = {
            item.work_item_id: setup.sandboxes[item.work_item_id] for item in review_work_items
        }
        summary = self.runtime.run(
            manifest_run_id=setup.run_id,
            work_items=review_work_items,
            sandboxes=review_sandboxes,
            output_root=run_root / "reviewer_results",
            event_log_path=run_root / "worker-events.jsonl",
            collector_results=dict(setup.collector_results),
        )
        if not isinstance(summary, ReviewRunSummary) or summary.run_id != setup.run_id:
            raise DiffReviewError("Reviewer runtime returned an invalid diff review summary")
        reviewed_validation = ResultValidator().validate(
            summary,
            review_coverage,
            controls,
            review_sandboxes,
            work_items=review_work_items,
            collector_results=setup.collector_results,
        )
        validation = merge_validations(
            setup.coverage,
            reviewed_validation,
            previous_validation,
            plan.reuse,
            previous_snapshot.run_id,
        )
        suspicious = _legacy_flag_set(validation)
        verifier = VerifierResult(status="not_required")
        previous_manual_ids = _manual_review_ids(
            setup.coverage, previous_validation, previous_snapshot
        )
        automated_regressions = _automated_evidence_regressions(
            setup.coverage, previous_validation, validation, plan
        )
        resolved = ComplianceResolver().resolve(controls, setup.coverage, validation)
        gate = CoverageGate().evaluate(
            controls,
            setup.coverage,
            validation,
            resolved,
            mode="diff",
            previous_manual_ids=sorted(previous_manual_ids),
            automated_evidence_regression_ids=sorted(automated_regressions),
        )
        snapshot = Snapshot(
            contract="compliance_snapshot.v1",
            run_id=setup.run_id,
            git_revision=stable_hash(
                {item.repo_id: item.git_revision or "unversioned" for item in setup.inventories}
            ),
            mode="diff",
            baseline_run_id=previous_snapshot.run_id,
            control_results=resolved,
            coverage_manifest_ref=f"runs/{setup.run_id}/coverage_manifest.json",
            applicability_hash=stable_hash(
                setup.applicability.model_dump(mode="json") if setup.applicability else None
            ),
            ci_status=gate.ci_status,
            reviewed_rows=[
                row.coverage_unit_id for row in gate.rows if row.result_origin == "reviewed"
            ],
            reused_rows=[
                row.coverage_unit_id for row in gate.rows if row.result_origin == "reused"
            ],
            missing_surfaces=setup.coverage.missing_surfaces,
            validation_flags=validation.flags,
            manual_review_new_ids=gate.manual_review_new_ids,
            manual_review_existing_ids=gate.manual_review_existing_ids,
            manual_review_resolved_ids=gate.manual_review_resolved_ids,
            automated_evidence_regression_ids=gate.automated_evidence_regression_ids,
            run_status="completed",
            repository_revisions={
                item.repo_id: item.git_revision or "unversioned" for item in setup.inventories
            },
            repository_fingerprints={
                item.repo_id: repository_input_fingerprint(item, setup.sandboxes)
                for item in setup.inventories
            },
            reuse_fingerprints=plan.fingerprints,
        )
        policies = {item.control_id: item.missing_evidence_policy for item in controls.controls}
        regressions = compare_regression(snapshot, previous_snapshot, policies)
        snapshot = snapshot.model_copy(
            update={
                "regressions": [
                    f"{item.control_id}:{item.reason}"
                    for item in regressions.changes
                    if item.classification in {"regression", "warning"}
                ]
            }
        )
        from compliance_review.review.full_review import render_markdown_report

        report_path = self.store.write_run_text(
            setup.run_id, "report.md", render_markdown_report(snapshot, gate)
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
        self.store.write_run_model(setup.run_id, "regressions.json", regressions)
        return DiffReviewRunResult(
            summary=summary,
            validation=validation,
            suspicious=suspicious,
            verifier=verifier,
            resolved_controls=resolved,
            coverage_gate=gate,
            snapshot=snapshot,
            report_path=report_path.as_posix(),
            diff_path=(run_root / "diff.json").as_posix(),
            impact_path=(run_root / "impact.json").as_posix(),
            reuse_plan_path=(run_root / "reuse-plan.json").as_posix(),
            regression_path=(run_root / "regressions.json").as_posix(),
        )


def merge_validations(
    coverage: CoverageSet,
    reviewed: ResultValidationResult,
    previous: ResultValidationResult,
    reuse: ReusePlan,
    previous_run_id: str,
) -> ResultValidationResult:
    reviewed_by_id = {item.row_id: item for item in reviewed.rows}
    previous_by_id = {item.row_id: item for item in previous.rows}
    reused_ids = set(reuse.reused_unit_ids)
    reviewed_ids = set(reuse.review_unit_ids)
    terminal_ids = set(reuse.terminal_non_review_unit_ids)
    rows: list[ValidatedReviewRow] = []
    errors: list[str] = []
    for unit in coverage.units:
        row_id = f"{unit.control_id}:{unit.surface}"
        if unit.coverage_unit_id in reviewed_ids:
            row = reviewed_by_id.get(row_id)
            if row is None:
                errors.append(f"{row_id}:missing_reviewer_row")
                row = ValidatedReviewRow(
                    row_id=row_id,
                    control_id=unit.control_id,
                    surface=unit.surface,
                    work_item_id=unit.work_item_id,
                    valid=False,
                    suspicious=False,
                )
            rows.append(row)
        elif unit.coverage_unit_id in reused_ids:
            previous_row = previous_by_id.get(row_id)
            if (
                previous_row is None
                or not previous_row.valid
                or previous_row.row is None
                or previous_row.row.evidence_status != "complete"
                or previous_row.row.recommended_control_status != "pass"
            ):
                errors.append(f"{row_id}:unsafe_reuse_row")
                rows.append(
                    ValidatedReviewRow(
                        row_id=row_id,
                        control_id=unit.control_id,
                        surface=unit.surface,
                        work_item_id=unit.work_item_id,
                        valid=False,
                        suspicious=False,
                    )
                )
            else:
                rows.append(
                    previous_row.model_copy(
                        update={"result_origin": "reused", "previous_run_id": previous_run_id}
                    )
                )
        elif unit.coverage_unit_id in terminal_ids:
            rows.append(
                ValidatedReviewRow(
                    row_id=row_id,
                    control_id=unit.control_id,
                    surface=unit.surface,
                    work_item_id=unit.work_item_id,
                    valid=True,
                    suspicious=False,
                )
            )
        else:
            errors.append(f"{row_id}:coverage_unit_not_planned")
    return ResultValidationResult(
        valid=not errors and reviewed.valid,
        rows=rows,
        flags={item.row_id: item.flags for item in rows if item.flags},
        suspicious_row_ids=[item.row_id for item in rows if item.suspicious],
        errors=[*reviewed.errors, *errors],
    )


def _filtered_work_items(work_items: Sequence[WorkItem], unit_ids: set[str]) -> list[WorkItem]:
    filtered: list[WorkItem] = []
    for item in work_items:
        selected_unit_ids = [unit_id for unit_id in item.coverage_unit_ids if unit_id in unit_ids]
        if not selected_unit_ids:
            continue
        selected_control_ids = [
            control_id
            for control_id, unit_id in zip(item.control_ids, item.coverage_unit_ids)
            if unit_id in unit_ids
        ]
        filtered.append(
            item.model_copy(
                update={"coverage_unit_ids": selected_unit_ids, "control_ids": selected_control_ids}
            )
        )
    return filtered


def _legacy_flag_set(validation: ResultValidationResult) -> SuspiciousReviewSet:
    return SuspiciousReviewSet(row_ids=sorted(validation.flags), reasons=validation.flags)


def _manual_review_ids(
    coverage: CoverageSet,
    validation: ResultValidationResult,
    snapshot: Snapshot | None = None,
) -> set[str]:
    if snapshot is not None:
        snapshot_manual_ids = (
            set(snapshot.manual_review_new_ids)
            | set(snapshot.manual_review_existing_ids)
        )
        if snapshot_manual_ids:
            return snapshot_manual_ids
    rows = {item.row_id: item for item in validation.rows}
    manual: set[str] = set()
    for unit in coverage.units:
        if is_automatable_surface(unit.surface):
            continue
        row = rows.get(f"{unit.control_id}:{unit.surface}")
        if row is None or row.row is None or row.row.evidence_status == "manual_required":
            manual.add(unit.coverage_unit_id)
    return manual


def _automated_evidence_regressions(
    coverage: CoverageSet,
    previous: ResultValidationResult,
    current: ResultValidationResult,
    plan: DiffReviewPlan,
) -> set[str]:
    previous_by_id = {item.row_id: item for item in previous.rows}
    current_by_id = {item.row_id: item for item in current.rows}
    decisions = {item.coverage_unit_id: item for item in plan.reuse.decisions}
    regressions: set[str] = set()
    for unit in coverage.units:
        if not is_automatable_surface(unit.surface):
            continue
        row_id = f"{unit.control_id}:{unit.surface}"
        old = previous_by_id.get(row_id)
        new = current_by_id.get(row_id)
        old_good = bool(
            old
            and old.valid
            and old.row is not None
            and old.row.evidence_status == "complete"
            and old.row.recommended_control_status == "pass"
        )
        new_bad = not bool(
            new
            and new.valid
            and new.row is not None
            and new.row.evidence_status == "complete"
        )
        decision = decisions.get(unit.coverage_unit_id)
        affected = bool(decision and not decision.reusable)
        if old_good and new_bad and affected:
            regressions.add(unit.coverage_unit_id)
    return regressions
