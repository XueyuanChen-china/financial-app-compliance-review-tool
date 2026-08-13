from pathlib import Path

import pytest

from compliance_review.repository import (
    GitRepository,
    ReadOnlyRepositoryTools,
    RepositorySandbox,
)
from compliance_review.repository.sandbox import SandboxViolation

FIXTURES = Path(__file__).parent / "fixtures" / "day2"


def test_sandbox_blocks_escape_and_reads_inside() -> None:
    sandbox = RepositorySandbox(FIXTURES)

    assert "android/AndroidManifest.xml" in sandbox.list_files("android/**/*")
    assert "READ_CONTACTS" in sandbox.read_text("android/AndroidManifest.xml")
    with pytest.raises(SandboxViolation):
        sandbox.resolve("../outside.txt")


def test_sandbox_hides_sensitive_files() -> None:
    sandbox = RepositorySandbox(FIXTURES)

    assert all(not path.endswith(".env") for path in sandbox.list_files("**/*"))
    with pytest.raises(SandboxViolation):
        sandbox.read_text(".env")


def test_search_code_works_without_git() -> None:
    tools = ReadOnlyRepositoryTools(RepositorySandbox(FIXTURES))

    matches = tools.search_code("/api/loan", roots=("backend",), file_globs=("*.py",))

    assert len(matches) == 1
    assert matches[0].path == "backend/app.py"
    assert matches[0].line_number == 2


def test_git_search_combines_allowed_root_and_file_glob(tmp_path: Path) -> None:
    import subprocess

    (tmp_path / "allowed").mkdir()
    (tmp_path / "forbidden").mkdir()
    (tmp_path / "allowed" / "inside.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "forbidden" / "outside.py").write_text("needle\n", encoding="utf-8")
    subprocess.run(("git", "init"), cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(("git", "add", "."), cwd=tmp_path, check=True, capture_output=True)

    tools = ReadOnlyRepositoryTools(RepositorySandbox(tmp_path))
    matches = tools.search_code("needle", roots=("allowed",), file_globs=("*.py",))

    assert [match.path for match in matches] == ["allowed/inside.py"]


def test_git_metadata_is_structured_for_non_git_fixture() -> None:
    metadata = GitRepository(FIXTURES).metadata()

    assert metadata.is_git_repository is False
    assert metadata.error_code == "path_is_inside_parent_repository"
