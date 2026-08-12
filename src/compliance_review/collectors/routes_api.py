from __future__ import annotations

import json
import re
from typing import Any

import yaml

from compliance_review.collectors.base import CollectorResult, status_for_inputs
from compliance_review.domain.models import Fact, SourceRef, Surface
from compliance_review.repository.sandbox import RepositorySandbox

ROUTE_PATTERNS = (
    re.compile(r"\bpath\s*:\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\b(?:axios|fetch)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)"),
    re.compile(r"@(?:Get|Post|Put|Delete|Patch)Mapping\s*\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\b(?:app|router)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)"),
)


class RouteApiCollector:
    collector_id = "route_api_inventory"

    def collect(
        self,
        sandbox: RepositorySandbox,
        roots: tuple[str, ...] = ("src",),
        source_surface: Surface = "frontend_h5",
        file_globs: tuple[str, ...] = (
            "*.js",
            "*.jsx",
            "*.ts",
            "*.tsx",
            "*.vue",
            "*.java",
            "*.kt",
            "*.py",
            "*.json",
            "*.yaml",
            "*.yml",
        ),
        limit: int = 500,
    ) -> CollectorResult:
        files = _files_under_roots(sandbox, roots, file_globs, limit)
        facts: list[Fact] = []
        parse_failures = 0
        for path in files:
            try:
                text = sandbox.read_text(path)
            except (OSError, ValueError):
                parse_failures += 1
                continue

            if source_surface == "backend_api_doc" and _is_api_document(path):
                try:
                    document = _load_api_document(path, text)
                except (ValueError, TypeError, yaml.YAMLError):
                    parse_failures += 1
                    continue
                for route, method, operation_id in _document_endpoints(document):
                    facts.append(
                        _endpoint_fact(
                            source_surface=source_surface,
                            path=path,
                            method=method,
                            route=route,
                            operation_id=operation_id,
                            fact_index=len(facts) + 1,
                        )
                    )
                continue

            lines = text.splitlines()
            for line_number, line in enumerate(lines, start=1):
                for pattern in ROUTE_PATTERNS:
                    for match in pattern.finditer(line):
                        method, route = _route_match(match)
                        facts.append(
                            _endpoint_fact(
                                source_surface=source_surface,
                                path=path,
                                method=method,
                                route=route,
                                line_number=line_number,
                                fact_index=len(facts) + 1,
                            )
                        )
        parser_status, coverage_status = status_for_inputs(files, parse_failures)
        if files and not facts:
            coverage_status = "unknown"
        return CollectorResult(
            collector_id=self.collector_id,
            source_surface=source_surface,
            parser_status=parser_status,
            coverage_status=coverage_status,
            input_files=files,
            facts=facts,
            limitations=[
                "endpoint extraction is a deterministic candidate inventory, "
                "not proof of runtime reachability or authorization behavior",
            ],
            metadata={
                "endpoint_count": len(facts),
                "roots": list(roots),
                "document_mode": source_surface == "backend_api_doc",
            },
        )


def _files_under_roots(
    sandbox: RepositorySandbox, roots: tuple[str, ...], globs: tuple[str, ...], limit: int
) -> list[str]:
    files = []
    for root in roots:
        for path in sandbox.list_files(f"{root}/**/*", limit=limit):
            if any(_matches(path, glob) for glob in globs):
                files.append(path)
                if len(files) >= limit:
                    return sorted(set(files))
    return sorted(set(files))


def _matches(path: str, glob: str) -> bool:
    return path.endswith(glob.removeprefix("*"))


def _route_match(match: re.Match[str]) -> tuple[str, str]:
    groups = match.groups()
    if len(groups) == 1:
        return "unknown", groups[0]
    return groups[0].upper(), groups[1]


def _is_api_document(path: str) -> bool:
    return path.lower().endswith((".json", ".yaml", ".yml"))


def _load_api_document(path: str, text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text) if path.lower().endswith(".json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to parse API document {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"API document {path} must contain an object")
    return payload


def _document_endpoints(document: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return []
    endpoints: list[tuple[str, str, str | None]] = []
    for route, operations in paths.items():
        if not isinstance(route, str) or not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch", "head", "options"}:
                continue
            operation_id = operation.get("operationId") if isinstance(operation, dict) else None
            endpoints.append(
                (route, method.upper(), operation_id if isinstance(operation_id, str) else None)
            )
    return endpoints


def _endpoint_fact(
    *,
    source_surface: Surface,
    path: str,
    method: str,
    route: str,
    operation_id: str | None = None,
    line_number: int | None = None,
    fact_index: int,
) -> Fact:
    observed_value: dict[str, str] = {"method": method, "route": route}
    if operation_id:
        observed_value["operation_id"] = operation_id
    return Fact(
        fact_id=f"fact.{source_surface}.route.{fact_index}",
        source_surface=source_surface,
        fact_type="route_or_api_endpoint",
        observed_value=observed_value,
        source_refs=[
            SourceRef(path=path, start_line=line_number, end_line=line_number)
        ],
        parser_status="ok",
        coverage_status="partial",
        evidence_strength="server_doc" if source_surface == "backend_api_doc" else "static_proof",
        limitations=[
            "candidate endpoint inventory; does not prove runtime reachability, "
            "authorization, or persistence"
        ],
    )
