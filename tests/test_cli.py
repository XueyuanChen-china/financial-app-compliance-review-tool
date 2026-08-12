from pathlib import Path

from typer.testing import CliRunner

from compliance_review import __version__
from compliance_review.cli import app
from compliance_review.config.loader import ConfigLoadError, load_controls, load_profile

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
    assert '"confirmation_status": "awaiting_confirmation"' in result.stdout
    assert (workspace / "setup" / "repository_inventory.json").is_file()
