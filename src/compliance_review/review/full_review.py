from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Mapping, Protocol

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
    SuspiciousRouter,
    TargetedVerifier,
)
from compliance_review.review.models import FullReviewRunResult, ReviewRunSummary, VerifierResult
from compliance_review.review.provider import ModelProvider
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
        suspicious = SuspiciousRouter().route(validation)
        verifier = (
            TargetedVerifier(self.verifier_provider).verify(suspicious, validation, controls)
            if self.verifier_provider is not None
            else VerifierResult(
                status="not_required" if not suspicious.row_ids else "failed",
                errors=[] if not suspicious.row_ids else ["verifier provider is unavailable"],
            )
        )
        resolved = ComplianceResolver().resolve(controls, setup.coverage, validation, verifier)
        gate = CoverageGate().evaluate(controls, setup.coverage, validation, resolved)
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
            missing_surfaces=setup.coverage.missing_surfaces,
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
        self.store.write_run_model(setup.run_id, "suspicious_rows.json", suspicious)
        self.store.write_run_model(setup.run_id, "verifier/verifier_result.json", verifier)
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
    lines = [
        "# Compliance Review Report",
        "",
        f"- Run: `{snapshot.run_id}`",
        f"- CI decision: **{snapshot.ci_status.upper()}**",
        f"- Coverage ledger complete: `{str(coverage_gate.complete).lower()}`",
        f"- Reviewed: `{len(snapshot.reviewed_rows)}`",
        f"- Reused: `{len(snapshot.reused_rows)}`",
        f"- Missing surfaces: {', '.join(snapshot.missing_surfaces) or 'none'}",
        "",
        "## Control Summary",
        "",
        "| control | severity | status | reason |",
        "|---|---|---|---|",
    ]
    for item in snapshot.control_results:
        lines.append(
            f"| `{item.control_id}` | {item.severity} | **{item.status}** | "
            f"{' '.join(item.reasons)} |"
        )
    lines.extend(
        [
            "",
            "## Coverage Manifest",
            "",
            "| coverage unit | surface | evidence | resolution | origin |",
            "|---|---|---|---|---|",
        ]
    )
    for row in coverage_gate.rows:
        lines.append(
            f"| `{row.coverage_unit_id}` | {row.surface} | {row.evidence_status} | "
            f"{row.resolution_status} | {row.result_origin} |"
        )
    lines.extend(
        [
            "",
            "## Machine Summary",
            "",
            f"Control counts: `{json.dumps(status_counts, sort_keys=True)}`",
            "",
            "This report is deterministically derived from snapshot.json and "
            "coverage_manifest.json; raw agent prose is not used as a reporting source.",
            "",
        ]
    )
    return "\n".join(lines)


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
