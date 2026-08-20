from __future__ import annotations

import json
from typing import Any

import yaml

from compliance_review.collectors.base import CollectorResult, status_for_inputs
from compliance_review.domain.models import Fact, SourceRef
from compliance_review.repository.sandbox import RepositorySandbox

_MAX_API_DOCUMENT_BYTES = 10_000_000


class ApiDocumentCollector:
    """Extract declared endpoints from OpenAPI/Swagger JSON or YAML documents.

    Source-code route discovery is intentionally outside this Collector. Graphify
    and the read-only Reviewer tools handle source navigation and verification.
    """

    collector_id = "api_document_inventory"

    def collect(
        self,
        sandbox: RepositorySandbox,
        roots: tuple[str, ...] = (".",),
        file_globs: tuple[str, ...] = ("*.json", "*.yaml", "*.yml"),
        limit: int = 500,
    ) -> CollectorResult:
        files = _files_under_roots(sandbox, roots, file_globs, limit)
        facts: list[Fact] = []
        parse_failures = 0
        for path in files:
            try:
                # API exports can be substantially larger than source snippets, but
                # still receive a bounded collector-specific read budget.
                document = _load_document(
                    path, sandbox.read_text(path, max_bytes=_MAX_API_DOCUMENT_BYTES)
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError):
                parse_failures += 1
                continue
            for route, method, operation_id in _document_endpoints(document):
                facts.append(
                    _endpoint_fact(
                        path=path,
                        method=method,
                        route=route,
                        operation_id=operation_id,
                        fact_index=len(facts) + 1,
                    )
                )

        parser_status, coverage_status = status_for_inputs(files, parse_failures)
        if files and not facts:
            coverage_status = "unknown"
        return CollectorResult(
            collector_id=self.collector_id,
            source_surface="backend_api_doc",
            parser_status=parser_status,
            coverage_status=coverage_status,
            input_files=files,
            facts=facts,
            limitations=[
                "API document inventory proves declared endpoints only; it does not prove "
                "backend implementation, runtime reachability, authorization, or persistence"
            ],
            metadata={
                "endpoint_count": len(facts),
                "roots": list(roots),
                "source_kind": "openapi_or_swagger_document",
            },
        )


def _files_under_roots(
    sandbox: RepositorySandbox, roots: tuple[str, ...], globs: tuple[str, ...], limit: int
) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    files: list[str] = []
    for root in roots:
        for path in sandbox.list_files(f"{root.rstrip('/')}/**/*", limit=limit):
            if any(path.endswith(glob.removeprefix("*")) for glob in globs):
                files.append(path)
                if len(files) >= limit:
                    return sorted(set(files))
    return sorted(set(files))


def _load_document(path: str, text: str) -> dict[str, Any]:
    payload = json.loads(text) if path.lower().endswith(".json") else yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise TypeError(f"API document {path} must contain an object")
    return payload


def _document_endpoints(document: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return []
    endpoints: list[tuple[str, str, str | None]] = []
    methods = {"get", "post", "put", "delete", "patch", "head", "options"}
    for route, operations in paths.items():
        if not isinstance(route, str) or not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() not in methods:
                continue
            operation_id = operation.get("operationId") if isinstance(operation, dict) else None
            endpoints.append(
                (route, method.upper(), operation_id if isinstance(operation_id, str) else None)
            )
    return endpoints


def _endpoint_fact(
    *,
    path: str,
    method: str,
    route: str,
    operation_id: str | None,
    fact_index: int,
) -> Fact:
    observed_value: dict[str, str] = {"method": method, "route": route}
    if operation_id:
        observed_value["operation_id"] = operation_id
    return Fact(
        fact_id=f"fact.backend_api_doc.endpoint.{fact_index}",
        source_surface="backend_api_doc",
        fact_type="declared_api_endpoint",
        observed_value=observed_value,
        source_refs=[SourceRef(path=path)],
        parser_status="ok",
        coverage_status="partial",
        evidence_strength="server_doc",
        limitations=[
            "declared API endpoint only; implementation and runtime behavior require "
            "backend_code evidence"
        ],
    )
