from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from compliance_review.domain.models import DiffFile, RepositoryDiff


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
            line[3:].split(" -> ")[-1].strip() for line in status.splitlines() if len(line) >= 4
        )
        return GitMetadata(
            revision=revision,
            is_git_repository=True,
            is_dirty=bool(status.strip()),
            changed_files=changed,
        )

    def diff(
        self,
        repo_id: str,
        base_revision: str | None,
        head_revision: str | None = None,
    ) -> RepositoryDiff:
        """Return a structured base-to-head diff without flattening repositories."""
        metadata = self.metadata()
        if not metadata.is_git_repository or metadata.revision is None:
            return RepositoryDiff(
                repo_id=repo_id,
                base_revision=base_revision,
                head_revision=metadata.revision,
                comparable=False,
                error_code=metadata.error_code or "not_a_git_repository",
            )
        if not base_revision:
            return RepositoryDiff(
                repo_id=repo_id,
                head_revision=head_revision or metadata.revision,
                comparable=False,
                error_code="base_revision_missing",
            )
        head = head_revision or metadata.revision
        if self._run(("rev-parse", "--verify", f"{base_revision}^{{commit}}")) is None:
            return RepositoryDiff(
                repo_id=repo_id,
                base_revision=base_revision,
                head_revision=head,
                comparable=False,
                error_code="base_revision_not_found",
            )
        if self._run(("rev-parse", "--verify", f"{head}^{{commit}}")) is None:
            return RepositoryDiff(
                repo_id=repo_id,
                base_revision=base_revision,
                head_revision=head,
                comparable=False,
                error_code="head_revision_not_found",
            )
        args: tuple[str, ...] = ("diff", "--name-status", "--find-renames", base_revision, head)
        if head == metadata.revision:
            args = ("diff", "--name-status", "--find-renames", base_revision)
        output = self._run(args)
        if output is None:
            return RepositoryDiff(
                repo_id=repo_id,
                base_revision=base_revision,
                head_revision=head,
                comparable=False,
                error_code="diff_failed",
            )
        files: list[DiffFile] = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            status = fields[0]
            if status.startswith("R") and len(fields) >= 3:
                files.append(
                    DiffFile(
                        repo_id=repo_id,
                        path=fields[2],
                        previous_path=fields[1],
                        change_type="rename",
                    )
                )
                continue
            change_types: dict[str, Literal["add", "modify", "delete", "rename"]] = {
                "A": "add",
                "M": "modify",
                "D": "delete",
            }
            change_type = change_types.get(status[:1])
            if change_type is not None:
                files.append(DiffFile(repo_id=repo_id, path=fields[1], change_type=change_type))
        tracked_paths = {item.path for item in files}
        status_output = self._run(("status", "--porcelain=v1", "--untracked-files=all")) or ""
        for line in status_output.splitlines():
            if line.startswith("?? "):
                path = line[3:].strip()
                if path not in tracked_paths:
                    files.append(DiffFile(repo_id=repo_id, path=path, change_type="add"))
        return RepositoryDiff(
            repo_id=repo_id,
            base_revision=base_revision,
            head_revision=head,
            comparable=True,
            files=files,
            working_tree_included=head == metadata.revision,
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
