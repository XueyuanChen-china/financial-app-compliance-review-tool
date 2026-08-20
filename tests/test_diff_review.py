from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from compliance_review.domain.models import (
    ApplicabilitySet,
    Control,
    ControlSet,
    ControlSurfaceResult,
    CoverageSet,
    CoverageUnit,
    EvidenceAnchor,
    Fact,
    ResolvedControlResult,
    ReviewResult,
    Snapshot,
    SourceRef,
    WorkItem,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.repository import GitRepository, RepositorySandbox
from compliance_review.review import FullReviewService
from compliance_review.review.diff_review import (
    DiffReviewPlanner,
    DiffReviewService,
    compare_regression,
    coverage_unit_fingerprint,
    repository_input_fingerprint,
)
from compliance_review.review.evidence import file_content_revision, normalize_snippet
from compliance_review.review.models import ReviewManifest, ReviewRunSummary, WorkerExecution
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.models import (
    AppFactSet,
    AppProfile,
    AppProfileField,
    ComplianceWorkspace,
    ProfileConfirmation,
    ProfileValidationResult,
    RepositoryInventory,
    WorkspaceRepository,
)
from compliance_review.setup.service import ReviewSetupResult


class ManifestRuntime:
    """Deterministic test runtime that validates the checked-in manifest content."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(
        self,
        manifest_run_id: str,
        work_items: list[WorkItem],
        sandboxes: Mapping[str, RepositorySandbox],
        output_root: Path,
        event_log_path: Path | None = None,
        collector_results: Mapping[str, object] | None = None,
    ) -> ReviewRunSummary:
        del event_log_path, collector_results
        output_root.mkdir(parents=True, exist_ok=True)
        executions: list[WorkerExecution] = []
        for work_item in work_items:
            self.calls.append(work_item.work_item_id)
            sandbox = sandboxes[work_item.work_item_id]
            path = "AndroidManifest.xml" if work_item.surface == "android_native" else "src/app.js"
            text = sandbox.read_text(path)
            snippet = text.strip()
            anchor_id = f"anchor.{work_item.work_item_id}"
            anchor = EvidenceAnchor(
                anchor_id=anchor_id,
                control_ids=work_item.control_ids,
                source_surface=work_item.surface,
                source_tool="read_file",
                path=path,
                start_line=1,
                end_line=len(text.splitlines()),
                exact_snippet=snippet,
                normalized_snippet_hash=hashlib.sha256(
                    normalize_snippet(snippet).encode("utf-8")
                ).hexdigest(),
                file_revision=file_content_revision(sandbox.read_bytes(path)),
                evidence_strength="static_proof",
                summary="Deterministic test evidence anchor.",
            )
            rows = []
            for control_id in work_item.control_ids:
                failed = control_id == "permission.contacts" and "READ_CONTACTS" in text
                rows.append(
                    ControlSurfaceResult(
                        control_id=control_id,
                        surface=work_item.surface,
                        evidence_status="complete",
                        recommended_control_status="fail" if failed else "pass",
                        observed_evidence_strength="static_proof",
                        anchor_ids=[anchor_id],
                        confidence="high",
                    )
                )
            attempt_id = f"attempt.{work_item.work_item_id}"
            result = ReviewResult(
                contract="review_result.v1",
                work_item_id=work_item.work_item_id,
                attempt_id=attempt_id,
                execution_status="completed",
                rows=rows,
                anchors=[anchor],
                agent_id="test-reviewer",
            )
            executions.append(
                WorkerExecution(
                    work_item_id=work_item.work_item_id,
                    attempt_id=attempt_id,
                    agent_id="test-reviewer",
                    execution_status="completed",
                    result_path=(output_root / f"{work_item.work_item_id}.json").as_posix(),
                    result=result,
                    context_fingerprint="deterministic-test",
                )
            )
        return ReviewRunSummary(
            run_id=manifest_run_id,
            executions=executions,
            max_concurrency=3,
            completed=len(executions),
            failed=0,
            event_log_path=(output_root / "events.jsonl").as_posix(),
        )


def test_manifest_read_contacts_diff_review_reruns_only_affected_unit(tmp_path: Path) -> None:
    android_root = _git_repository(
        tmp_path / "android", "AndroidManifest.xml", '<manifest package="example" />\n'
    )
    frontend_root = _git_repository(
        tmp_path / "frontend", "src/app.js", "export const disclosure = true;\n"
    )
    workspace_root = tmp_path / "workspace"
    controls = _controls()
    runtime = ManifestRuntime()
    baseline_setup = _setup(workspace_root, android_root, frontend_root, "baseline", "full")
    _write_controls(workspace_root, controls)

    baseline = FullReviewService(workspace_root, runtime).run(baseline_setup, controls)
    assert {item.status for item in baseline.snapshot.control_results} == {"pass"}
    assert baseline.snapshot.reuse_fingerprints
    assert baseline.snapshot.semantic_baseline_run_id == "baseline"

    (android_root / "AndroidManifest.xml").write_text(
        '<manifest package="example">\n'
        '  <uses-permission android:name="android.permission.READ_CONTACTS" />\n'
        "</manifest>\n",
        encoding="utf-8",
    )
    runtime.calls.clear()
    current_setup = _setup(workspace_root, android_root, frontend_root, "incremental", "diff")
    _write_controls(workspace_root, controls)
    result = DiffReviewService(workspace_root, runtime).run(
        current_setup, controls, baseline.snapshot, baseline.validation
    )

    assert runtime.calls == ["wi.android.permissions.android_native"]
    assert result.snapshot.reviewed_rows == ["cu.permission.contacts.android_native"]
    assert result.snapshot.reused_rows == ["cu.frontend.notice.frontend_h5"]
    assert result.snapshot.reviewer_work_items_completed == 1
    assert result.snapshot.reviewer_work_items_failed == 0
    assert result.snapshot.baseline_run_id == "baseline"
    assert result.snapshot.semantic_baseline_run_id == "baseline"
    origins = {row.coverage_unit_id: row.result_origin for row in result.coverage_gate.rows}
    assert origins["cu.permission.contacts.android_native"] == "reviewed"
    assert origins["cu.frontend.notice.frontend_h5"] == "carried_forward"
    assert result.snapshot.ci_status == "block"
    assert any(
        item["control_id"] == "permission.contacts" and item["classification"] == "regression"
        for item in json.loads(Path(result.regression_path).read_text())["changes"]
    )
    assert Path(result.diff_path).is_file()
    assert Path(result.impact_path).is_file()
    assert Path(result.reuse_plan_path).is_file()


def test_same_surface_unaffected_unit_is_carried_after_unrelated_code_change(
    tmp_path: Path,
) -> None:
    setup, controls, baseline = _baseline(tmp_path)
    android_root = Path(setup.inventories[0].path)
    (android_root / "res").mkdir()
    (android_root / "res" / "colors.xml").write_text("<color name=\"brand\">red</color>\n")
    current = _setup(
        tmp_path / "workspace",
        android_root,
        Path(setup.inventories[1].path),
        "diff-unaffected",
        "diff",
    )
    provider = StaticModelProvider(
        lambda request: {
            "content": json.dumps(
                {
                    "coverage_unit_id": _impact_coverage_unit_id(request.messages[1]["content"]),
                    "status": "unaffected",
                    "reasons": ["New color resource does not affect manifest permission evidence."],
                    "changed_file_refs": ["res/colors.xml"],
                }
            )
        }
    )

    result = DiffReviewService(
        tmp_path / "workspace", ManifestRuntime(), impact_provider=provider
    ).run(
        current, controls, baseline.snapshot, baseline.validation
    )

    assert result.snapshot.reviewer_work_items_completed == 0
    assert "cu.permission.contacts.android_native" in result.snapshot.reused_rows

    chained_setup = _setup(
        tmp_path / "workspace",
        android_root,
        Path(setup.inventories[1].path),
        "diff-chain",
        "diff",
    )
    chained_runtime = ManifestRuntime()
    chained = DiffReviewService(tmp_path / "workspace", chained_runtime).run(
        chained_setup, controls, result.snapshot, result.validation
    )

    assert chained_runtime.calls == []
    carried_row = next(
        row
        for row in chained.validation.rows
        if row.control_id == "permission.contacts" and row.surface == "android_native"
    )
    assert carried_row.result_origin == "carried_forward"
    assert carried_row.previous_run_id == "diff-unaffected"
    assert chained.snapshot.baseline_run_id == "diff-unaffected"
    assert chained.snapshot.semantic_baseline_run_id == "baseline"
    assert carried_row.result_origin_run_id == "baseline"


def _impact_coverage_unit_id(payload: object) -> str:
    assert isinstance(payload, str)
    return json.loads(payload)["coverage_unit_id"]


def test_git_diff_keeps_repo_identity_for_same_surface_repositories(tmp_path: Path) -> None:
    first = _git_repository(tmp_path / "first", "service.py", "one\n")
    second = _git_repository(tmp_path / "second", "service.py", "two\n")
    first_revision = GitRepository(first).metadata().revision
    second_revision = GitRepository(second).metadata().revision
    (first / "service.py").write_text("changed\n", encoding="utf-8")

    first_diff = GitRepository(first).diff("backend-a", first_revision)
    second_diff = GitRepository(second).diff("backend-b", second_revision)

    assert first_diff.files[0].repo_id == "backend-a"
    assert first_diff.files[0].path == "service.py"
    assert second_diff.files == []


def test_graphify_output_does_not_invalidate_incremental_review(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repository", "service.py", "one\n")
    metadata_before = GitRepository(repository).metadata()
    fingerprint_before = repository_input_fingerprint(
        RepositoryInventory(
            repo_id="repository",
            path=repository.as_posix(),
            detected_surface="backend_code",
            surface_status="confirmed",
        ),
        {},
    )

    (repository / "graphify-out" / "cache").mkdir(parents=True)
    (repository / "graphify-out" / "cache" / "graph.json").write_text(
        "{}\n", encoding="utf-8"
    )

    metadata_after = GitRepository(repository).metadata()
    diff = GitRepository(repository).diff("repository", metadata_before.revision)
    fingerprint_after = repository_input_fingerprint(
        RepositoryInventory(
            repo_id="repository",
            path=repository.as_posix(),
            detected_surface="backend_code",
            surface_status="confirmed",
        ),
        {},
    )

    assert metadata_after.is_dirty is False
    assert metadata_after.changed_files == ()
    assert diff.files == []
    assert fingerprint_after == fingerprint_before


def test_git_metadata_does_not_invalidate_incremental_review(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repository", "service.py", "one\n")
    worktree = tmp_path / "worktree"
    subprocess.run(
        ("git", "worktree", "add", "--detach", worktree.as_posix(), "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        original = repository_input_fingerprint(
            RepositoryInventory(
                repo_id="repository",
                path=repository.as_posix(),
                detected_surface="backend_code",
                surface_status="confirmed",
            ),
            {},
        )
        detached = repository_input_fingerprint(
            RepositoryInventory(
                repo_id="worktree",
                path=worktree.as_posix(),
                detected_surface="backend_code",
                surface_status="confirmed",
            ),
            {},
        )
        assert detached == original
    finally:
        subprocess.run(
            ("git", "worktree", "remove", "--force", worktree.as_posix()),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )


def test_reuse_fingerprint_changes_for_control_profile_and_evidence_inputs(tmp_path: Path) -> None:
    android_root = _git_repository(tmp_path / "android", "AndroidManifest.xml", "<manifest />\n")
    frontend_root = _git_repository(tmp_path / "frontend", "src/app.js", "export const x = 1;\n")
    setup = _setup(tmp_path / "workspace", android_root, frontend_root, "run", "full")
    controls = _controls()
    unit = setup.coverage.units[0]
    baseline = coverage_unit_fingerprint(
        unit,
        controls,
        setup.profile,
        setup.applicability,
        setup.app_facts,
        setup.inventories,
        setup.sandboxes,
    )
    changed_control = controls.model_copy(
        update={
            "controls": [
                controls.controls[0].model_copy(update={"title": "Changed control"}),
                controls.controls[1],
            ]
        }
    )
    changed_profile = setup.profile.model_copy(
        update={
            "fields": {
                **setup.profile.fields,
                "business_type": AppProfileField(
                    value=["marketplace"], source="human_confirmed", confidence="high"
                ),
            }
        }
    )
    assert baseline != coverage_unit_fingerprint(
        unit,
        changed_control,
        setup.profile,
        setup.applicability,
        setup.app_facts,
        setup.inventories,
        setup.sandboxes,
    )
    assert baseline != coverage_unit_fingerprint(
        unit,
        controls,
        changed_profile,
        setup.applicability,
        setup.app_facts,
        setup.inventories,
        setup.sandboxes,
    )


def test_unchanged_compatible_units_are_planned_for_reuse(tmp_path: Path) -> None:
    setup, controls, baseline = _baseline(tmp_path)
    current = _setup(
        tmp_path / "workspace",
        Path(setup.inventories[0].path),
        Path(setup.inventories[1].path),
        "diff",
        "diff",
    )

    plan = DiffReviewPlanner().plan(current, controls, baseline.snapshot, baseline.validation)

    assert plan.reuse.review_unit_ids == []
    assert set(plan.reuse.reused_unit_ids) == {
        "cu.permission.contacts.android_native",
        "cu.frontend.notice.frontend_h5",
    }


def test_control_or_required_strength_change_disables_reuse(tmp_path: Path) -> None:
    setup, controls, baseline = _baseline(tmp_path)
    current = _setup(
        tmp_path / "workspace",
        Path(setup.inventories[0].path),
        Path(setup.inventories[1].path),
        "diff",
        "diff",
    )
    changed_control = controls.model_copy(
        update={
            "controls": [
                controls.controls[0].model_copy(
                    update={"minimum_evidence_strength": {"android_native": "static_proof"}}
                ),
                controls.controls[1],
            ]
        }
    )

    plan = DiffReviewPlanner().plan(
        current, changed_control, baseline.snapshot, baseline.validation
    )

    assert "cu.permission.contacts.android_native" in plan.reuse.review_unit_ids


def test_profile_or_collector_fact_change_disables_reuse(tmp_path: Path) -> None:
    setup, controls, baseline = _baseline(tmp_path)
    current = _setup(
        tmp_path / "workspace",
        Path(setup.inventories[0].path),
        Path(setup.inventories[1].path),
        "diff",
        "diff",
    )
    changed_profile = current.profile.model_copy(
        update={
            "fields": {
                **current.profile.fields,
                "business_type": AppProfileField(
                    value=["marketplace"], source="human_confirmed", confidence="high"
                ),
            }
        }
    )
    with_profile_change = replace(current, profile=changed_profile)
    profile_plan = DiffReviewPlanner().plan(
        with_profile_change, controls, baseline.snapshot, baseline.validation
    )
    changed_fact = Fact(
        fact_id="fact.android.permission",
        repo_id="android",
        source_surface="android_native",
        fact_type="android_manifest_permission",
        observed_value="READ_CONTACTS",
        source_refs=[SourceRef(path="AndroidManifest.xml")],
        parser_status="ok",
        coverage_status="complete",
        evidence_strength="declared",
    )
    with_fact_change = replace(current, app_facts=AppFactSet(facts=[changed_fact]))
    fact_plan = DiffReviewPlanner().plan(
        with_fact_change, controls, baseline.snapshot, baseline.validation
    )

    assert "cu.permission.contacts.android_native" in profile_plan.reuse.review_unit_ids
    assert "cu.permission.contacts.android_native" in fact_plan.reuse.review_unit_ids


def test_invalid_previous_row_is_carried_forward_when_code_is_unchanged(tmp_path: Path) -> None:
    setup, controls, baseline = _baseline(tmp_path)
    current = _setup(
        tmp_path / "workspace",
        Path(setup.inventories[0].path),
        Path(setup.inventories[1].path),
        "diff",
        "diff",
    )
    invalid_rows = [
        item.model_copy(update={"valid": False})
        if item.control_id == "permission.contacts"
        else item
        for item in baseline.validation.rows
    ]
    invalid_validation = baseline.validation.model_copy(update={"rows": invalid_rows})

    plan = DiffReviewPlanner().plan(current, controls, baseline.snapshot, invalid_validation)

    assert "cu.permission.contacts.android_native" in plan.reuse.reused_unit_ids


def test_regression_rules_are_deterministic() -> None:
    previous = _snapshot_with_status("baseline", "pass")
    failed = _snapshot_with_status("current", "fail")
    warning = _snapshot_with_status("current", "indeterminate")
    improved = _snapshot_with_status("current", "pass", previous_status="fail")

    fail_result = compare_regression(failed, previous, {"permission.contacts": "block"})
    warn_result = compare_regression(warning, previous, {"permission.contacts": "warn"})
    block_result = compare_regression(warning, previous, {"permission.contacts": "block"})
    improvement = compare_regression(
        improved,
        _snapshot_with_status("baseline", "fail"),
        {"permission.contacts": "block"},
    )

    assert fail_result.ci_status == "block"
    assert warn_result.ci_status == "warn"
    assert block_result.ci_status == "block"
    assert improvement.changes[0].classification == "improvement"


def _baseline(tmp_path: Path):  # type: ignore[no-untyped-def]
    android_root = _git_repository(tmp_path / "android", "AndroidManifest.xml", "<manifest />\n")
    frontend_root = _git_repository(tmp_path / "frontend", "src/app.js", "export const x = 1;\n")
    workspace_root = tmp_path / "workspace"
    setup = _setup(workspace_root, android_root, frontend_root, "baseline", "full")
    controls = _controls()
    _write_controls(workspace_root, controls)
    baseline = FullReviewService(workspace_root, ManifestRuntime()).run(setup, controls)
    return setup, controls, baseline


def _snapshot_with_status(run_id: str, status: str, previous_status: str | None = None) -> Snapshot:
    del previous_status
    return Snapshot(
        contract="compliance_snapshot.v1",
        run_id=run_id,
        git_revision="revision",
        mode="diff",
        control_results=[
            ResolvedControlResult(
                control_id="permission.contacts",
                status=status,  # type: ignore[arg-type]
                severity="low",
                coverage_unit_ids=["cu.permission.contacts.android_native"],
            )
        ],
        coverage_manifest_ref="runs/test/coverage_manifest.json",
        applicability_hash="hash",
        ci_status="pass",
        run_status="completed",
    )


def _controls() -> ControlSet:
    return ControlSet(
        contract="control_set.v1",
        version="1",
        controls=[
            Control(
                control_id="permission.contacts",
                module_id="permissions",
                title="Contacts permission must not be declared.",
                severity="low",
                applicability_expression="self_lending == true",
                required_surfaces=["android_native"],
                minimum_evidence_strength={"android_native": "declared"},
                missing_evidence_policy="block",
                source_refs=[SourceRef(url="https://example.test/policy")],
                reuse_invalidation_keys=["manifest", "profile"],
            ),
            Control(
                control_id="frontend.notice",
                module_id="notice",
                title="Frontend notice remains available.",
                severity="low",
                applicability_expression="self_lending == true",
                required_surfaces=["frontend_h5"],
                minimum_evidence_strength={"frontend_h5": "declared"},
                missing_evidence_policy="warn",
                source_refs=[SourceRef(url="https://example.test/policy")],
                reuse_invalidation_keys=["frontend"],
            ),
        ],
    )


def _setup(
    workspace_root: Path,
    android_root: Path,
    frontend_root: Path,
    run_id: str,
    mode: str,
) -> ReviewSetupResult:
    android_metadata = GitRepository(android_root).metadata()
    frontend_metadata = GitRepository(frontend_root).metadata()
    inventories = [
        RepositoryInventory(
            repo_id="android",
            path=android_root.as_posix(),
            declared_surface="android_native",
            detected_surface="android_native",
            detected_surfaces=["android_native"],
            surface_status="confirmed",
            git_revision=android_metadata.revision,
            is_git_repository=True,
            is_dirty=android_metadata.is_dirty,
            changed_files=list(android_metadata.changed_files),
        ),
        RepositoryInventory(
            repo_id="frontend",
            path=frontend_root.as_posix(),
            declared_surface="frontend_h5",
            detected_surface="frontend_h5",
            detected_surfaces=["frontend_h5"],
            surface_status="confirmed",
            git_revision=frontend_metadata.revision,
            is_git_repository=True,
            is_dirty=frontend_metadata.is_dirty,
            changed_files=list(frontend_metadata.changed_files),
        ),
    ]
    profile = AppProfile(
        version="1",
        status="confirmed",
        fields={
            "self_lending": AppProfileField(
                value=True, source="human_confirmed", confidence="high"
            ),
            "business_type": AppProfileField(
                value=["personal_loan"], source="human_confirmed", confidence="high"
            ),
        },
    )
    coverage = CoverageSet(
        profile_version="1",
        control_version="1",
        units=[
            CoverageUnit(
                coverage_unit_id="cu.permission.contacts.android_native",
                control_id="permission.contacts",
                module_id="permissions",
                surface="android_native",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="declared",
                reason="test",
                work_item_id="wi.android.permissions.android_native",
            ),
            CoverageUnit(
                coverage_unit_id="cu.frontend.notice.frontend_h5",
                control_id="frontend.notice",
                module_id="notice",
                surface="frontend_h5",
                applicability_status="applicable",
                coverage_status="planned",
                required_evidence_strength="declared",
                reason="test",
                work_item_id="wi.frontend.notice.frontend_h5",
            ),
        ],
    )
    work_items = [
        WorkItem(
            work_item_id="wi.android.permissions.android_native",
            module_id="permissions",
            repository_id="android",
            repository_ids=["android"],
            surface="android_native",
            control_ids=["permission.contacts"],
            coverage_unit_ids=["cu.permission.contacts.android_native"],
            allowed_roots=["."],
        ),
        WorkItem(
            work_item_id="wi.frontend.notice.frontend_h5",
            module_id="notice",
            repository_id="frontend",
            repository_ids=["frontend"],
            surface="frontend_h5",
            control_ids=["frontend.notice"],
            coverage_unit_ids=["cu.frontend.notice.frontend_h5"],
            allowed_roots=["."],
        ),
    ]
    store = ArtifactStore(workspace_root)
    store.prepare_run_artifacts(run_id)
    workspace = ComplianceWorkspace(
        workspace_root=workspace_root.as_posix(),
        repositories=[
            WorkspaceRepository(repo_id="android", path=android_root.as_posix()),
            WorkspaceRepository(repo_id="frontend", path=frontend_root.as_posix()),
        ],
    )
    return ReviewSetupResult(
        workspace=workspace,
        inventories=inventories,
        app_facts=AppFactSet(inventory_ids=["android", "frontend"]),
        profile=profile,
        profile_validation=ProfileValidationResult(valid=True),
        confirmation=ProfileConfirmation(status="confirmed"),
        applicability=ApplicabilitySet(profile_version="1", control_version="1", decisions=[]),
        coverage=coverage,
        manifest=ReviewManifest(
            contract="review_manifest.v1",
            run_id=run_id,
            mode=mode,  # type: ignore[arg-type]
            source_profile_version="1",
            source_control_version="1",
        ),
        run_id=run_id,
        work_items=work_items,
        sandboxes={
            "wi.android.permissions.android_native": RepositorySandbox(android_root),
            "wi.frontend.notice.frontend_h5": RepositorySandbox(frontend_root),
        },
    )


def _write_controls(workspace_root: Path, controls: ControlSet) -> None:
    from compliance_review.compilation.models import ControlValidationResult

    store = ArtifactStore(workspace_root)
    store.write_controls(controls)
    store.write_control_validation(ControlValidationResult(valid=True, validated_control_count=2))


def _git_repository(root: Path, relative_path: str, content: str) -> Path:
    root.mkdir(parents=True)
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    for args in (
        ("git", "init"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
        ("git", "add", "."),
        ("git", "commit", "-m", "baseline"),
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)
    return root


def test_git_diff_records_old_and_new_hunk_ranges(tmp_path: Path) -> None:
    repository = _git_repository(
        tmp_path / "android-hunks",
        "AndroidManifest.xml",
        "<manifest>\n  <uses-permission android:name=\"OLD\" />\n</manifest>\n",
    )
    baseline = GitRepository(repository).metadata().revision
    assert baseline is not None
    (repository / "AndroidManifest.xml").write_text(
        "<manifest>\n  <uses-permission android:name=\"NEW\" />\n</manifest>\n",
        encoding="utf-8",
    )

    diff = GitRepository(repository).diff("android", baseline)

    assert diff.comparable is True
    assert len(diff.files) == 1
    assert diff.files[0].old_hunks[0].start_line == 2
    assert diff.files[0].old_hunks[0].line_count == 1
    assert diff.files[0].new_hunks[0].start_line == 2
    assert diff.files[0].new_hunks[0].line_count == 1
