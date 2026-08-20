from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from compliance_review.domain.models import ChangedHunk, DiffFile, RepositoryDiff

_GENERATED_REPOSITORY_PREFIXES = ("graphify-out/",)
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def is_generated_repository_artifact(path: str) -> bool:
    """Return whether a path is produced by the review tool, not source code."""
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized == "graphify-out" or any(
        normalized.startswith(prefix) for prefix in _GENERATED_REPOSITORY_PREFIXES
    )


def is_repository_metadata(path: str) -> bool:
    """Return whether a path belongs to VCS metadata, not reviewable source."""
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized == ".git" or normalized.startswith(".git/")


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
            path
            for line in status.splitlines()
            if len(line) >= 4
            for path in [line[3:].split(" -> ")[-1].strip()]
            if not is_generated_repository_artifact(path)
        )
        return GitMetadata(
            revision=revision,
            is_git_repository=True,
            is_dirty=bool(changed),
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
                if is_generated_repository_artifact(fields[2]):
                    continue
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
            if change_type is not None and not is_generated_repository_artifact(fields[1]):
                files.append(DiffFile(repo_id=repo_id, path=fields[1], change_type=change_type))
        files = [self._with_hunks(item, base_revision) for item in files]
        tracked_paths = {item.path for item in files}
        status_output = self._run(("status", "--porcelain=v1", "--untracked-files=all")) or ""
        for line in status_output.splitlines():
            if line.startswith("?? "):
                path = line[3:].strip()
                if path not in tracked_paths and not is_generated_repository_artifact(path):
                    files.append(self._untracked_file(repo_id, path))
        return RepositoryDiff(
            repo_id=repo_id,
            base_revision=base_revision,
            head_revision=head,
            comparable=True,
            files=files,
            working_tree_included=head == metadata.revision,
            code_state_id=self.code_state_id(),
        )

    def code_state_id(self) -> str | None:
        """Hash current reviewable state without depending on a baseline revision.

        The digest includes HEAD, staged/unstaged changes, and untracked file
        content.  Generated Graphify output and Git internals never affect it.
        """
        metadata = self.metadata()
        if not metadata.is_git_repository or metadata.revision is None:
            return None
        digest = hashlib.sha256()
        digest.update(metadata.revision.encode("utf-8"))
        patch = self._run_bytes(("diff", "--binary", "HEAD"))
        if patch is None:
            return None
        digest.update(patch)
        untracked = self._run_bytes(("ls-files", "--others", "--exclude-standard", "-z"))
        if untracked is None:
            return None
        for raw_path in sorted(path for path in untracked.split(b"\0") if path):
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if is_generated_repository_artifact(path) or is_repository_metadata(path):
                continue
            digest.update(path.encode("utf-8", errors="surrogateescape"))
            try:
                digest.update((self.root / path).read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
        return digest.hexdigest()

    def _with_hunks(self, diff_file: DiffFile, base_revision: str) -> DiffFile:
        paths = [diff_file.path]
        if diff_file.previous_path and diff_file.previous_path != diff_file.path:
            paths.append(diff_file.previous_path)
        output = self._run(("diff", "--unified=0", "--find-renames", base_revision, "--", *paths))
        if output is None:
            return diff_file
        old_hunks: list[ChangedHunk] = []
        new_hunks: list[ChangedHunk] = []
        for line in output.splitlines():
            match = _HUNK_HEADER.match(line)
            if match is None:
                continue
            old_hunks.append(
                ChangedHunk(
                    start_line=int(match.group("old_start")),
                    line_count=int(match.group("old_count") or "1"),
                )
            )
            new_hunks.append(
                ChangedHunk(
                    start_line=int(match.group("new_start")),
                    line_count=int(match.group("new_count") or "1"),
                )
            )
        return diff_file.model_copy(update={"old_hunks": old_hunks, "new_hunks": new_hunks})

    def _untracked_file(self, repo_id: str, path: str) -> DiffFile:
        try:
            line_count = len((self.root / path).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            line_count = 0
        return DiffFile(
            repo_id=repo_id,
            path=path,
            change_type="add",
            new_hunks=[ChangedHunk(start_line=1, line_count=line_count)],
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

    def _run_bytes(self, args: tuple[str, ...]) -> bytes | None:
        try:
            result = subprocess.run(
                ("git", *args),
                cwd=self.root,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout
