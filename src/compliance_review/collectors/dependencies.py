from __future__ import annotations

import json
import re
from typing import Optional

from compliance_review.collectors.base import CollectorResult, status_for_inputs
from compliance_review.domain.models import Fact, SourceRef, Surface
from compliance_review.repository.sandbox import RepositorySandbox

GRADLE_DEPENDENCY_RE = re.compile(
    r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s*"
    r"[\(\s][\"']([^\"']+)[\"']"
)


class DependencyCollector:
    collector_id = "dependency_inventory"

    def collect(
        self,
        sandbox: RepositorySandbox,
        input_files: tuple[str, ...] = (
            "package.json",
            "app/build.gradle",
            "app/build.gradle.kts",
            "build.gradle",
            "build.gradle.kts",
        ),
        source_surface: Surface = "android_native",
    ) -> CollectorResult:
        found: list[str] = []
        failures = 0
        facts: list[Fact] = []
        for path in input_files:
            try:
                text = sandbox.read_text(path)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                failures += 1
                continue
            found.append(path)
            if path.endswith("package.json"):
                parsed, failed = _parse_package_json(text, path, source_surface)
            else:
                parsed, failed = _parse_gradle(text, path, source_surface)
            facts.extend(parsed)
            failures += failed

        parser_status, coverage_status = status_for_inputs(found, failures)
        limitations = []
        if not facts and found:
            limitations.append("no supported dependency declarations were found")
        return CollectorResult(
            collector_id=self.collector_id,
            source_surface=source_surface,
            parser_status=parser_status,
            coverage_status=coverage_status,
            input_files=found,
            facts=facts,
            limitations=limitations,
            metadata={"dependency_count": len(facts)},
        )


def _parse_package_json(text: str, path: str, surface: Surface) -> tuple[list[Fact], int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], 1
    facts = []
    for group in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = data.get(group, {})
        if not isinstance(values, dict):
            continue
        for name, version in sorted(values.items()):
            facts.append(_dependency_fact(path, surface, str(name), str(version), group))
    return facts, 0


def _parse_gradle(text: str, path: str, surface: Surface) -> tuple[list[Fact], int]:
    facts = []
    for declaration in GRADLE_DEPENDENCY_RE.findall(text):
        parts = declaration.split(":")
        name = ":".join(parts[:2]) if len(parts) >= 2 else declaration
        version = parts[2] if len(parts) >= 3 else None
        facts.append(_dependency_fact(path, surface, name, version, "gradle"))
    return facts, 0


def _dependency_fact(
    path: str, surface: Surface, name: str, version: Optional[str], group: str
) -> Fact:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return Fact(
        fact_id=f"fact.{surface}.dependency.{key}",
        source_surface=surface,
        fact_type="dependency_declaration",
        observed_value={"name": name, "version": version, "group": group},
        source_refs=[SourceRef(path=path)],
        parser_status="ok",
        coverage_status="complete",
        evidence_strength="static_proof",
    )
