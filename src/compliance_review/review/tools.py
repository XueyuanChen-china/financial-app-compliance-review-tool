from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from compliance_review.code_map import (
    CodeMapPath,
    CodeMapPathResult,
    CodeMapProvider,
    CodeMapQuery,
    CodeMapQueryResult,
    GraphifyCodeMapProvider,
)
from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import WorkItem
from compliance_review.repository import ReadOnlyRepositoryTools, RepositorySandbox
from compliance_review.review.models import ScopedToolResult, ToolCall


class ScopedToolExecutor:
    """Execute only read-only tools inside one Work Item's allowed roots."""

    def __init__(
        self,
        sandbox: RepositorySandbox,
        work_item: WorkItem,
        code_map_provider: CodeMapProvider | None = None,
        collector_results: dict[str, CollectorResult] | None = None,
        max_tool_calls: int | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.work_item = work_item
        self.tools = ReadOnlyRepositoryTools(sandbox)
        self.read_paths: set[str] = set()
        self.code_map_provider = code_map_provider or GraphifyCodeMapProvider(
            sandbox.root
        )
        self.collector_results = collector_results or {}
        self.tool_calls = 0
        self.max_tool_calls = max_tool_calls or max(3, work_item.max_tool_rounds * 3)

    def execute(self, call: ToolCall) -> ScopedToolResult:
        try:
            self.tool_calls += 1
            if self.tool_calls > self.max_tool_calls:
                raise ValueError("work item max_tool_calls exceeded")
            output: Any
            if call.name == "code_map_query":
                output = self._code_map_query(call.arguments)
            elif call.name == "code_map_path":
                output = self._code_map_path(call.arguments)
            elif call.name == "get_collector_facts":
                output = self._get_collector_facts(call.arguments)
            elif call.name == "list_files":
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

    def _code_map_query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = _string_argument(arguments, "query")
        surface = arguments.get("surface", self.work_item.surface)
        if surface != self.work_item.surface:
            raise ValueError("code map surface must match the Work Item surface")
        request = CodeMapQuery.model_validate(
            {
                "query": query,
                "surface": surface,
                "max_candidates": arguments.get("max_candidates", 5),
                "budget": arguments.get("budget", 2000),
            }
        )
        result = self.code_map_provider.query(request)
        return self._bounded_code_map_query(result).model_dump()

    def _code_map_path(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = _string_argument(arguments, "source")
        target = _string_argument(arguments, "target")
        surface = arguments.get("surface", self.work_item.surface)
        if surface != self.work_item.surface:
            raise ValueError("code map surface must match the Work Item surface")
        request = CodeMapPath.model_validate(
            {
                "source": source,
                "target": target,
                "surface": surface,
                "max_hops": arguments.get("max_hops", 6),
                "budget": arguments.get("budget", 2000),
            }
        )
        result = self.code_map_provider.path(request)
        return self._bounded_code_map_path(result).model_dump()

    def _get_collector_facts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        collector_id = arguments.get("collector_id")
        if collector_id is not None and not isinstance(collector_id, str):
            raise TypeError("collector_id must be a string")
        raw_fact_ids = arguments.get("fact_ids", [])
        if not isinstance(raw_fact_ids, list) or not all(
            isinstance(value, str) for value in raw_fact_ids
        ):
            raise TypeError("fact_ids must be a list of strings")
        fact_type = arguments.get("fact_type")
        if fact_type is not None and not isinstance(fact_type, str):
            raise TypeError("fact_type must be a string")
        limit = _bounded_int(arguments, "limit", 50, 1, 100)
        compatible_results = {
            current_id: result
            for current_id, result in self.collector_results.items()
            if result.source_surface == self.work_item.surface
        }
        if collector_id is None:
            selected = compatible_results
        else:
            selected = (
                {collector_id: compatible_results[collector_id]}
                if collector_id in compatible_results
                else {}
            )
        facts: list[dict[str, Any]] = []
        for result in selected.values():
            for fact in result.facts:
                if raw_fact_ids and fact.fact_id not in raw_fact_ids:
                    continue
                if fact_type and fact.fact_type != fact_type:
                    continue
                facts.append(fact.model_dump())
        facts = facts[:limit]
        missing_fact_ids = [
            fact_id
            for fact_id in raw_fact_ids
            if not any(fact["fact_id"] == fact_id for fact in facts)
        ]
        return {
            "collector_id": collector_id,
            "available_collectors": sorted(compatible_results),
            "facts": facts,
            "missing_fact_ids": missing_fact_ids,
            "limitations": [
                limitation
                for result in selected.values()
                for limitation in result.limitations
            ][:limit],
        }

    def _bounded_code_map_query(self, result: CodeMapQueryResult) -> CodeMapQueryResult:
        candidates = [
            candidate
            for candidate in result.candidates
            if self._is_allowed_code_map_candidate(candidate.path)
        ]
        symbols = {candidate.symbol for candidate in candidates}
        relations = [
            relation
            for relation in result.relations
            if relation.source in symbols and relation.target in symbols
        ]
        return result.model_copy(update={"candidates": candidates, "relations": relations})

    def _bounded_code_map_path(self, result: CodeMapPathResult) -> CodeMapPathResult:
        nodes = [
            node
            for node in result.nodes
            if self._is_allowed_code_map_candidate(node.path)
        ]
        symbols = {node.symbol for node in nodes}
        relations = [
            relation
            for relation in result.relations
            if relation.source in symbols and relation.target in symbols
        ]
        return result.model_copy(update={"nodes": nodes, "relations": relations})

    def _is_allowed_code_path(self, path: str) -> bool:
        try:
            self._allowed_path(path)
            return True
        except ValueError:
            return False

    def _is_allowed_code_map_candidate(self, path: str | None) -> bool:
        if path is None:
            return self.work_item.allowed_roots in ([], ["."])
        return self._is_allowed_code_path(path)

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
