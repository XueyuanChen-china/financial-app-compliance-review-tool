from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from compliance_review.domain.models import WorkItem
from compliance_review.repository import ReadOnlyRepositoryTools, RepositorySandbox
from compliance_review.review.models import ScopedToolResult, ToolCall


class ScopedToolExecutor:
    """Execute only read-only tools inside one Work Item's allowed roots."""

    def __init__(
        self,
        sandbox: RepositorySandbox,
        work_item: WorkItem,
    ) -> None:
        self.sandbox = sandbox
        self.work_item = work_item
        self.tools = ReadOnlyRepositoryTools(sandbox)
        self.read_paths: set[str] = set()

    def execute(self, call: ToolCall) -> ScopedToolResult:
        try:
            output: Any
            if call.name == "list_files":
                output = self._list_files(call.arguments)
            elif call.name == "search_code":
                output = self._search_code(call.arguments)
            elif call.name == "read_file":
                output = self._read_file(call.arguments)
            else:
                raise ValueError(f"unsupported tool: {call.name}")
            return ScopedToolResult(call_id=call.call_id, name=call.name, ok=True, output=output)
        except (OSError, TypeError, ValueError) as exc:
            return ScopedToolResult(
                call_id=call.call_id,
                name=call.name,
                ok=False,
                error=str(exc),
            )

    def _list_files(self, arguments: dict[str, Any]) -> list[str]:
        pattern = _string_argument(arguments, "pattern", "**/*")
        limit = _bounded_int(arguments, "limit", 100, 1, 100)
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("list_files pattern leaves the allowed roots")
        paths: list[str] = []
        for root in self.work_item.allowed_roots or ["."]:
            root_path = self._allowed_path(root)
            combined = _join_pattern(root_path, pattern)
            paths.extend(self.sandbox.list_files(combined, limit=limit))
            if len(paths) >= limit:
                break
        return sorted(set(paths))[:limit]

    def _search_code(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        query = _string_argument(arguments, "query")
        root = _string_argument(arguments, "root", "")
        file_globs = arguments.get("file_globs", ())
        if not isinstance(file_globs, (list, tuple)):
            raise TypeError("file_globs must be a list")
        limit = _bounded_int(arguments, "limit", 100, 1, 100)
        roots: tuple[str, ...]
        if root:
            roots = (self._allowed_path(root),)
        else:
            roots = tuple(
                self._allowed_path(allowed) for allowed in self.work_item.allowed_roots
            )
        matches = self.tools.search_code(
            query,
            roots=roots or (".",),
            file_globs=tuple(str(glob) for glob in file_globs),
            limit=limit,
        )
        return [match.__dict__ for match in matches]

    def _read_file(self, arguments: dict[str, Any]) -> str:
        path = _string_argument(arguments, "path")
        self._allowed_path(path)
        if path not in self.read_paths and len(self.read_paths) >= self.work_item.max_files_read:
            raise ValueError("work item max_files_read exceeded")
        self.read_paths.add(path)
        start_line = _bounded_int(arguments, "start_line", 1, 1, 1_000_000)
        line_count = _bounded_int(
            arguments,
            "line_count",
            self.work_item.max_lines_per_read,
            1,
            self.work_item.max_lines_per_read,
        )
        return self.tools.read_file(path, start_line=start_line, line_count=line_count)

    def _allowed_path(self, path: str) -> str:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("path leaves the allowed roots")
        resolved = self.sandbox.resolve(path)
        relative = resolved.relative_to(self.sandbox.root).as_posix()
        allowed_roots = self.work_item.allowed_roots or ["."]
        for allowed in allowed_roots:
            allowed_resolved = self.sandbox.resolve(allowed)
            try:
                resolved.relative_to(allowed_resolved)
                return relative
            except ValueError:
                continue
        raise ValueError(f"path is outside work item roots: {path}")


def _join_pattern(root: str, pattern: str) -> str:
    if root in ("", "."):
        return pattern
    return f"{root.rstrip('/')}/{pattern}"


def _string_argument(arguments: dict[str, Any], name: str, default: str | None = None) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _bounded_int(
    arguments: dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return cast(int, value)


def serialize_tool_result(result: ScopedToolResult) -> str:
    return json.dumps(result.model_dump(), ensure_ascii=False, sort_keys=True)
