from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitMetadata:
    revision: str | None
    is_git_repository: bool
    is_dirty: bool
    changed_files: tuple[str, ...]
    error_code: str | None = None


class GitRepository:
    """Read-only Git metadata provider for a repository root."""

    def __init__(self, root: Path, timeout_seconds: float = 5.0) -> None:
        self.root = root
        self.timeout_seconds = timeout_seconds

    def metadata(self) -> GitMetadata:
        revision = self._run(("rev-parse", "HEAD"))
        repository_root = self._run(("rev-parse", "--show-toplevel"))
        if revision is None or repository_root is None:
            return GitMetadata(
                revision=None,
                is_git_repository=False,
                is_dirty=False,
                changed_files=(),
                error_code="not_a_git_repository",
            )
        if Path(repository_root).resolve() != self.root.resolve():
            return GitMetadata(
                revision=None,
                is_git_repository=False,
                is_dirty=False,
                changed_files=(),
                error_code="path_is_inside_parent_repository",
            )
        status = self._run(("status", "--porcelain=v1", "--untracked-files=all")) or ""
        changed = tuple(
            line[3:].split(" -> ")[-1].strip()
            for line in status.splitlines()
            if len(line) >= 4
        )
        return GitMetadata(
            revision=revision,
            is_git_repository=True,
            is_dirty=bool(status.strip()),
            changed_files=changed,
        )

    def _run(self, args: tuple[str, ...]) -> str | None:
        try:
            result = subprocess.run(
                ("git", *args),
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()
