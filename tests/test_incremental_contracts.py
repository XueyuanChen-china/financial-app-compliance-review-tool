from __future__ import annotations

import pytest

from compliance_review.domain.models import (
    ChangedHunk,
    CoverageManifestRow,
    DiffFile,
    DiffReviewWorkItem,
    FullReviewWorkItem,
    ReviewInputBaseline,
    ReviewInputFingerprint,
)
from compliance_review.review.input_baseline import collect_review_input_baseline
from compliance_review.review.manifest import ReviewWorkItemBuilder


def test_diff_work_item_requires_baseline_and_change_context() -> None:
    with pytest.raises(ValueError, match="baseline_context"):
        DiffReviewWorkItem(
            work_item_id="wi.diff.permissions.android_native",
            module_id="permissions",
            repository_id="android",
            surface="android_native",
            control_ids=["permission.contacts"],
            coverage_unit_ids=["cu.permission.contacts.android_native"],
            allowed_roots=["."],
        )


def test_full_work_item_rejects_diff_context() -> None:
    with pytest.raises(ValueError, match="must not include diff-only"):
        FullReviewWorkItem(
            work_item_id="wi.full.permissions.android_native",
            module_id="permissions",
            repository_id="android",
            surface="android_native",
            control_ids=["permission.contacts"],
            coverage_unit_ids=["cu.permission.contacts.android_native"],
            allowed_roots=["."],
            change_context={"changed_files": ["AndroidManifest.xml"]},
        )


def test_input_baseline_reports_missing_and_changed_artifacts() -> None:
    baseline = ReviewInputBaseline(
        run_id="baseline",
        artifacts=[
            ReviewInputFingerprint(
                artifact_id="controls", category="controls", path="setup/controls.json", sha256="a"
            ),
            ReviewInputFingerprint(
                artifact_id="profile",
                category="app_profile",
                path="setup/app_profile.json",
                sha256="b",
            ),
        ],
    )
    current = [
        ReviewInputFingerprint(
            artifact_id="controls",
            category="controls",
            path="setup/controls.json",
            sha256="changed",
        )
    ]

    result = baseline.compare(current)

    assert result.full_review_required is True
    assert result.changed_artifact_ids == ["controls"]
    assert result.missing_artifact_ids == ["profile"]


def test_changed_hunk_keeps_old_and_new_ranges() -> None:
    diff_file = DiffFile(
        repo_id="android",
        path="AndroidManifest.xml",
        previous_path="AndroidManifest.xml",
        change_type="modify",
        old_hunks=[ChangedHunk(start_line=8, line_count=2)],
        new_hunks=[ChangedHunk(start_line=8, line_count=3)],
    )

    assert diff_file.old_hunks[0].end_line == 9
    assert diff_file.new_hunks[0].end_line == 10


def test_carried_forward_row_keeps_immediate_and_original_origin() -> None:
    row = CoverageManifestRow(
        coverage_unit_id="cu.permission.contacts.android_native",
        control_id="permission.contacts",
        surface="android_native",
        execution_status="completed",
        evidence_status="partial",
        result_origin="carried_forward",
        previous_run_id="run-b",
        result_origin_run_id="run-a",
        resolution_status="indeterminate",
    )

    assert row.previous_run_id == "run-b"
    assert row.result_origin_run_id == "run-a"


def test_input_baseline_hashes_external_materials_by_surface(tmp_path) -> None:
    setup = tmp_path / "setup"
    setup.mkdir()
    for name in ("sources", "obligations", "controls", "app_profile", "repository_inventory"):
        (setup / f"{name}.json").write_text("{}", encoding="utf-8")
    api_document = tmp_path / "openapi.json"
    api_document.write_text('{"openapi":"3.0.0"}', encoding="utf-8")
    (tmp_path / "workspace.json").write_text(
        '{"workspace_root":".","repositories":[],"materials":['
        '{"path":"' + api_document.as_posix() + '","surface":"backend_api_doc"}]}' ,
        encoding="utf-8",
    )

    baseline = collect_review_input_baseline(tmp_path, "run-1")

    assert {item.category for item in baseline.artifacts} >= {"controls", "api_documents"}


def test_work_item_builder_adds_diff_context_without_changing_scope() -> None:
    item = FullReviewWorkItem(
        work_item_id="wi.full.permissions.android_native",
        module_id="permissions",
        repository_id="android",
        surface="android_native",
        control_ids=["permission.contacts"],
        coverage_unit_ids=["cu.permission.contacts.android_native"],
        allowed_roots=["."],
    )

    result = ReviewWorkItemBuilder().build_diff(
        item,
        baseline_context={"baseline_run_id": "run-a"},
        change_context={"changed_files": ["AndroidManifest.xml"]},
    )

    assert result.mode == "diff"
    assert result.control_ids == item.control_ids
    assert result.coverage_unit_ids == item.coverage_unit_ids
