from pathlib import Path

from typer.testing import CliRunner

from compliance_review import __version__
from compliance_review.cli import app
from compliance_review.config.loader import ConfigLoadError, load_controls, load_profile
from compliance_review.domain.models import ReviewInputBaseline, ReviewInputFingerprint, Snapshot
from compliance_review.persistence import ArtifactStore

PROJECT_ROOT = Path(__file__).parents[1]


def test_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_example_contracts_load() -> None:
    profile = load_profile(PROJECT_ROOT / "examples/app-profile.yaml")
    controls = load_controls(PROJECT_ROOT / "examples/mvp-controls.yaml")

    assert profile.app_name == "ForiQarz"
    assert len(controls.controls) == 8


def test_invalid_control_contract_is_rejected() -> None:
    try:
        load_controls(PROJECT_ROOT / "examples/invalid-controls.yaml")
    except ConfigLoadError as exc:
        assert "invalid ControlSet" in str(exc)
    else:
        raise AssertionError("invalid control fixture was accepted")


def test_validate_command() -> None:
    result = CliRunner().invoke(
        app,
        [
            "validate",
            "--profile",
            str(PROJECT_ROOT / "examples/app-profile.yaml"),
            "--controls",
            str(PROJECT_ROOT / "examples/mvp-controls.yaml"),
        ],
    )

    assert result.exit_code == 0
    assert "valid: profile=ForiQarz controls=8" in result.stdout


def test_init_workspace_setup_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = CliRunner().invoke(
        app,
        [
            "init",
            str(workspace),
            "--repository",
            f"web={PROJECT_ROOT / 'tests' / 'fixtures' / 'day2' / 'frontend'}",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert '"confirmation_status": "deferred_to_applicability"' in result.stdout
    assert (workspace / "setup" / "repository_inventory.json").is_file()


def test_diff_review_stops_before_setup_when_non_code_inputs_changed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "setup").mkdir(parents=True)
    (workspace / "workspace.json").write_text(
        '{"workspace_root":".","repositories":[]}', encoding="utf-8"
    )
    (workspace / "setup" / "controls.json").write_text('{"changed":true}', encoding="utf-8")
    ArtifactStore(workspace).write_review_input_baseline(
        "baseline",
        ReviewInputBaseline(
            run_id="baseline",
            artifacts=[
                ReviewInputFingerprint(
                    artifact_id="controls",
                    category="controls",
                    path="setup/controls.json",
                    sha256="old-hash",
                )
            ],
        ),
    )
    ArtifactStore(workspace).write_run_model(
        "baseline",
        "snapshot.json",
        Snapshot(
            contract="compliance_snapshot.v1",
            run_id="baseline",
            git_revision="revision",
            mode="full",
            semantic_baseline_run_id="baseline",
            coverage_manifest_ref="runs/baseline/coverage_manifest.json",
            applicability_hash="hash",
            ci_status="pass",
            run_status="completed",
        ),
    )

    result = CliRunner().invoke(
        app, ["diff-review", str(workspace), "--baseline-run-id", "baseline"]
    )

    assert result.exit_code == 3
    assert "FULL_REVIEW_REQUIRED=true" in result.stdout
    assert '"controls"' in result.stdout


def test_diff_review_requires_a_new_full_baseline_when_input_baseline_is_missing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = CliRunner().invoke(
        app, ["diff-review", str(workspace), "--baseline-run-id", "legacy"]
    )

    assert result.exit_code == 3
    assert "FULL_REVIEW_REQUIRED=true" in result.stdout
    assert "review-input-baseline.json" in result.stdout
