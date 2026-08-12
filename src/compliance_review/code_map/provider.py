from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Protocol, Sequence, Tuple

from compliance_review.code_map.lifecycle import GraphifyLifecycle
from compliance_review.code_map.models import (
    CodeMapCandidate,
    CodeMapPath,
    CodeMapPathResult,
    CodeMapQuery,
    CodeMapQueryResult,
    CodeMapRelation,
    CodeMapStatus,
)

_NODE_RE = re.compile(
    r"^NODE (?P<symbol>.+?) \[src=(?P<path>.*?) loc=(?P<location>[^ ]*).*]$"
)
_EDGE_RE = re.compile(
    r"^EDGE (?P<source>.+?) --(?P<relation>[^ ]+) "
    r"\[(?P<confidence>[^\]]+)]--> (?P<target>.+?)(?: at=(?P<source_ref>.*))?$"
)
_LINE_RE = re.compile(r"L(?P<start>\d+)(?:[-:]L?(?P<end>\d+))?$")


class CodeMapProvider(Protocol):
    """Stable interface exposed to reviewers; provider internals stay hidden."""

    def query(self, request: CodeMapQuery) -> CodeMapQueryResult:
        """Return a compact, bounded code-map response."""

    def path(self, request: CodeMapPath) -> CodeMapPathResult:
        """Return a compact path between two graph symbols."""


class GraphifyCodeMapProvider:
    """Call the local Graphify CLI and normalize its text output.

    This adapter deliberately does not expose Graphify's graph schema or skill.
    It also fails open: missing CLI, timeout, non-zero exit, and unparsable
    output become a bounded status result instead of a compliance conclusion.
    """

    def __init__(
        self,
        repo_path: Path,
        command: Sequence[str] = ("graphify",),
        timeout_seconds: float = 10.0,
        require_index: bool = True,
    ) -> None:
        self.repo_path = repo_path
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.require_index = require_index

    def query(self, request: CodeMapQuery) -> CodeMapQueryResult:
        if not self.repo_path.is_dir():
            return self._status_result(request, "unavailable", "repository_not_found")
        if not Path(self.command[0]).is_file() and shutil.which(self.command[0]) is None:
            return self._status_result(request, "unavailable", "graphify_not_found")
        if self.require_index and not GraphifyLifecycle.graph_paths(self.repo_path):
            return self._status_result(request, "unavailable", "graph_not_initialized")

        command = [
            *self.command,
            "query",
            request.query,
            "--budget",
            str(request.budget),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return self._status_result(request, "unavailable", "graphify_not_found")
        except subprocess.TimeoutExpired:
            return self._status_result(request, "degraded", "graphify_timeout")
        except OSError:
            return self._status_result(request, "unavailable", "graphify_os_error")

        if completed.returncode != 0:
            return self._status_result(request, "degraded", "graphify_command_failed")

        candidates, relations, truncated = self._parse_output(
            completed.stdout, request.max_candidates
        )
        if not candidates and "No matching nodes found" not in completed.stdout:
            return self._status_result(request, "degraded", "graphify_output_unparseable")

        return CodeMapQueryResult(
            query=request.query,
            surface=request.surface,
            provider="graphify",
            status="available",
            candidates=candidates,
            relations=relations,
            truncated=truncated,
        )

    def path(self, request: CodeMapPath) -> CodeMapPathResult:
        if not self.repo_path.is_dir():
            return self._path_status_result(request, "unavailable", "repository_not_found")
        if not Path(self.command[0]).is_file() and shutil.which(self.command[0]) is None:
            return self._path_status_result(request, "unavailable", "graphify_not_found")
        if self.require_index and not GraphifyLifecycle.graph_paths(self.repo_path):
            return self._path_status_result(request, "unavailable", "graph_not_initialized")

        command = [
            *self.command,
            "path",
            request.source,
            request.target,
            "--budget",
            str(request.budget),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return self._path_status_result(request, "unavailable", "graphify_not_found")
        except subprocess.TimeoutExpired:
            return self._path_status_result(request, "degraded", "graphify_timeout")
        except OSError:
            return self._path_status_result(request, "unavailable", "graphify_os_error")

        if completed.returncode != 0:
            return self._path_status_result(request, "degraded", "graphify_command_failed")
        nodes, relations, truncated = self._parse_path_output(
            completed.stdout, request.source, request.target, request.max_hops
        )
        if not nodes:
            return self._path_status_result(request, "degraded", "graphify_path_unparseable")
        return CodeMapPathResult(
            source=request.source,
            target=request.target,
            surface=request.surface,
            status="available",
            nodes=nodes,
            relations=relations,
            truncated=truncated,
        )

    @staticmethod
    def _status_result(
        request: CodeMapQuery, status: CodeMapStatus, error_code: str
    ) -> CodeMapQueryResult:
        return CodeMapQueryResult(
            query=request.query,
            surface=request.surface,
            provider="graphify",
            status=status,
            error_code=error_code,
        )

    @staticmethod
    def _path_status_result(
        request: CodeMapPath, status: CodeMapStatus, error_code: str
    ) -> CodeMapPathResult:
        return CodeMapPathResult(
            source=request.source,
            target=request.target,
            surface=request.surface,
            status=status,
            error_code=error_code,
        )

    @staticmethod
    def _parse_output(
        output: str, max_candidates: int
    ) -> tuple[list[CodeMapCandidate], list[CodeMapRelation], bool]:
        candidates: list[CodeMapCandidate] = []
        relations: list[CodeMapRelation] = []
        for line in output.splitlines():
            node_match = _NODE_RE.match(line.strip())
            if node_match:
                start_line, end_line = _parse_location(node_match.group("location"))
                candidates.append(
                    CodeMapCandidate(
                        symbol=node_match.group("symbol"),
                        path=node_match.group("path") or None,
                        start_line=start_line,
                        end_line=end_line,
                    )
                )
                continue

            edge_match = _EDGE_RE.match(line.strip())
            if edge_match:
                confidence_and_context = edge_match.group("confidence").split(" context=", 1)
                relations.append(
                    CodeMapRelation(
                        source=edge_match.group("source"),
                        relation=edge_match.group("relation"),
                        target=edge_match.group("target"),
                        confidence=confidence_and_context[0] or None,
                        source_path=_source_path(edge_match.group("source_ref")),
                        source_line=_source_line(edge_match.group("source_ref")),
                    )
                )

        truncated = len(candidates) > max_candidates
        candidates = candidates[:max_candidates]
        candidate_symbols = {candidate.symbol for candidate in candidates}
        relations = [
            relation
            for relation in relations
            if relation.source in candidate_symbols and relation.target in candidate_symbols
        ][: max_candidates * 2]
        return candidates, relations, truncated

    @staticmethod
    def _parse_path_output(
        output: str, source: str, target: str, max_hops: int
    ) -> tuple[list[CodeMapCandidate], list[CodeMapRelation], bool]:
        """Normalize Graphify path text without exposing its raw output."""
        symbols: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("Path:", "No path", "(") ):
                continue
            if "NODE " in stripped:
                node_match = _NODE_RE.match(stripped)
                if node_match:
                    symbols.append(node_match.group("symbol"))
                    continue
            for separator in (" → ", " -> ", " -> "):
                if separator in stripped:
                    symbols.extend(part.strip() for part in stripped.split(separator))
                    break
        if not symbols:
            return [], [], False
        normalized: list[str] = []
        for symbol in [source, *symbols, target]:
            clean = symbol.strip().rstrip(".")
            if clean and (not normalized or normalized[-1] != clean):
                normalized.append(clean)
        truncated = len(normalized) > max_hops + 1
        normalized = normalized[: max_hops + 1]
        nodes = [CodeMapCandidate(symbol=symbol) for symbol in normalized]
        relations = [
            CodeMapRelation(source=left, relation="path", target=right)
            for left, right in zip(normalized, normalized[1:])
        ]
        return nodes, relations, truncated


def _parse_location(location: str) -> Tuple[Optional[int], Optional[int]]:
    if not location:
        return None, None
    match = _LINE_RE.match(location)
    if not match:
        return None, None
    start = int(match.group("start"))
    end = int(match.group("end")) if match.group("end") else start
    return start, end


def _source_path(source_ref: Optional[str]) -> Optional[str]:
    if not source_ref or ":" not in source_ref:
        return source_ref or None
    return source_ref.rsplit(":", 1)[0] or None


def _source_line(source_ref: Optional[str]) -> Optional[int]:
    if not source_ref or ":" not in source_ref:
        return None
    location = source_ref.rsplit(":", 1)[1]
    start, _ = _parse_location(location)
    return start


def command_from_string(value: str) -> tuple[str, ...]:
    """Parse a configured command without invoking a shell."""
    command = tuple(shlex.split(value))
    if not command:
        raise ValueError("Graphify command must not be empty")
    return command
