from __future__ import annotations

import xml.etree.ElementTree as ET

from compliance_review.collectors.base import CollectorResult, status_for_inputs
from compliance_review.domain.models import Fact, SourceRef
from compliance_review.repository.sandbox import RepositorySandbox

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


class ManifestCollector:
    collector_id = "android_manifest"

    def collect(
        self, sandbox: RepositorySandbox, manifest_path: str = "app/src/main/AndroidManifest.xml"
    ) -> CollectorResult:
        try:
            text = sandbox.read_text(manifest_path)
        except FileNotFoundError:
            return CollectorResult(
                collector_id=self.collector_id,
                source_surface="android_native",
                parser_status="failed",
                coverage_status="unknown",
                limitations=[f"manifest not found: {manifest_path}"],
            )
        except (OSError, ValueError) as exc:
            return CollectorResult(
                collector_id=self.collector_id,
                source_surface="android_native",
                parser_status="failed",
                coverage_status="unknown",
                input_files=[manifest_path],
                limitations=[f"manifest unreadable: {exc}"],
            )

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            return CollectorResult(
                collector_id=self.collector_id,
                source_surface="android_native",
                parser_status="failed",
                coverage_status="unknown",
                input_files=[manifest_path],
                limitations=[f"manifest XML parse failed: {exc}"],
            )

        facts: list[Fact] = []
        for permission in root.findall("uses-permission"):
            name = permission.attrib.get(f"{ANDROID_NS}name")
            if name:
                facts.append(
                    _manifest_fact(
                        manifest_path,
                        "android_manifest_permission",
                        name,
                        f"permission:{name}",
                    )
                )

        application = root.find("application")
        component_counts = {
            "activity": 0,
            "service": 0,
            "receiver": 0,
            "provider": 0,
        }
        if application is not None:
            for component in component_counts:
                component_counts[component] = len(application.findall(component))
        for component, count in component_counts.items():
            facts.append(
                _manifest_fact(
                    manifest_path,
                    "android_manifest_component_count",
                    {"component": component, "count": count},
                    f"component-count:{component}",
                )
            )

        parser_status, coverage_status = status_for_inputs([manifest_path], 0)
        return CollectorResult(
            collector_id=self.collector_id,
            source_surface="android_native",
            parser_status=parser_status,
            coverage_status=coverage_status,
            input_files=[manifest_path],
            facts=facts,
            metadata={
                "permission_count": sum(
                    f.fact_type == "android_manifest_permission" for f in facts
                )
            },
        )


def _manifest_fact(path: str, fact_type: str, value: object, suffix: str) -> Fact:
    return Fact(
        fact_id=f"fact.android.{suffix.replace(':', '.')}",
        source_surface="android_native",
        fact_type=fact_type,
        observed_value=value,
        source_refs=[SourceRef(path=path)],
        parser_status="ok",
        coverage_status="complete",
        evidence_strength="static_proof",
    )
