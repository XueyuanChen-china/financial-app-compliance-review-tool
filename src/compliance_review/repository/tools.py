from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from compliance_review.repository.sandbox import RepositorySandbox, is_sensitive_path


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line_number: int
    line_text: str


class ReadOnlyRepositoryTools:
    """Bounded list/search/read operations for Reviewer and Collectors."""

    def __init__(self, sandbox: RepositorySandbox, timeout_seconds: float = 5.0) -> None:
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds

    def list_files(self, pattern: str = "*", limit: int = 500) -> list[str]:
        return self.sandbox.list_files(pattern, limit)

    def read_file(self, path: str, start_line: int = 1, line_count: int = 300) -> str:
        if start_line < 1 or line_count < 1:
            raise ValueError("start_line and line_count must be positive")
        lines = self.sandbox.read_text(path).splitlines()
        start = start_line - 1
        return "\n".join(lines[start : start + line_count])

    def search_code(
        self,
        query: str,
        roots: tuple[str, ...] = (),
        file_globs: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[SearchMatch]:
        if not query or limit < 1:
            raise ValueError("query must be non-empty and limit must be positive")
        safe_roots = tuple(
            str(self.sandbox.resolve(root).relative_to(self.sandbox.root)) for root in roots
        )
        matches = self._git_grep(query, safe_roots, file_globs, limit)
        if matches is not None:
            return matches
        return self._text_search(query, safe_roots, file_globs, limit)

    def _git_grep(
        self,
        query: str,
        roots: tuple[str, ...],
        file_globs: tuple[str, ...],
        limit: int,
    ) -> list[SearchMatch] | None:
        command = ["git", "grep", "-n", "-I", "-F", "--", query]
        command.extend(roots or (".",))
        for glob in file_globs:
            command.extend((":(glob)" + glob,))
        try:
            result = subprocess.run(
                command,
                cwd=self.sandbox.root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            return None
        return [
            match
            for match in _parse_grep_output(result.stdout, limit * 2)
            if not is_sensitive_path(match.path)
        ][:limit]

    def _text_search(
        self,
        query: str,
        roots: tuple[str, ...],
        file_globs: tuple[str, ...],
        limit: int,
    ) -> list[SearchMatch]:
        paths = []
        for root in roots or (".",):
            paths.extend(self.sandbox.list_files(f"{root}/**/*", limit=limit * 4))
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results = []
        for path in sorted(set(paths)):
            if file_globs and not any(_glob_match(path, glob) for glob in file_globs):
                continue
            try:
                lines = self.sandbox.read_text(path).splitlines()
            except (OSError, UnicodeError, ValueError):
                continue
            for number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    results.append(SearchMatch(path, number, line[:500]))
                    if len(results) >= limit:
                        return results
        return results


def _parse_grep_output(output: str, limit: int) -> list[SearchMatch]:
    results = []
    for line in output.splitlines():
        match = re.match(r"^(.*?):(\d+):(.*)$", line)
        if not match:
            continue
        results.append(SearchMatch(match.group(1), int(match.group(2)), match.group(3)[:500]))
        if len(results) >= limit:
            break
    return results


def _glob_match(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern)
