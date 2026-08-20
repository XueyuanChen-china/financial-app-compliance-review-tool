from __future__ import annotations

from pathlib import Path

from compliance_review.collectors import (
    ApiDocumentCollector,
    DependencyCollector,
    ManifestCollector,
)
from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import Fact, SourceRef, Surface
from compliance_review.repository import RepositorySandbox
from compliance_review.setup.models import AppFactSet, RepositoryInventory, WorkspaceMaterial

_SIGNAL_SURFACES: dict[str, Surface] = {
    "android_manifest": "android_native",
    "gradle_build": "android_native",
    "frontend_framework": "frontend_h5",
    "package_manifest": "frontend_h5",
    "backend_build_or_layout": "backend_code",
    "api_document": "backend_api_doc",
}


def collect_app_facts(
    inventories: list[RepositoryInventory],
    materials: list[WorkspaceMaterial] | tuple[WorkspaceMaterial, ...] = (),
) -> AppFactSet:
    facts: list[Fact] = []
    collector_results: list[dict[str, object]] = []
    for inventory in inventories:
        sandbox = RepositorySandbox(Path(inventory.path))
        primary = inventory.detected_surface or inventory.declared_surface
        if primary is not None:
            facts.append(
                Fact(
                    fact_id=f"fact.{inventory.repo_id}.surface",
                    repo_id=inventory.repo_id,
                    source_surface=primary,
                    fact_type="repository_surface",
                    observed_value={
                        "declared": inventory.declared_surface,
                        "detected": inventory.detected_surfaces,
                        "status": inventory.surface_status,
                    },
                    source_refs=[SourceRef(path=inventory.path)],
                    parser_status="ok",
                    coverage_status=(
                        "complete" if inventory.surface_status == "confirmed" else "unknown"
                    ),
                    evidence_strength="static_proof",
                )
            )
        for index, signal in enumerate(inventory.detection_signals, start=1):
            signal_surface = _SIGNAL_SURFACES.get(signal.signal_type, primary)
            if signal_surface is None:
                continue
            fact_type = {
                "frontend_framework": "frontend_framework",
                "backend_build_or_layout": "backend_presence",
                "api_document": "api_document_availability",
            }.get(signal.signal_type, "repository_detection_signal")
            source_path = (
                f"{inventory.path}/{signal.path}"
                if signal.path
                else inventory.path
            )
            facts.append(
                Fact(
                    fact_id=f"fact.{inventory.repo_id}.detection.{index}",
                    repo_id=inventory.repo_id,
                    source_surface=signal_surface,
                    fact_type=fact_type,
                    observed_value={
                        "signal_type": signal.signal_type,
                        "detail": signal.detail,
                    },
                    source_refs=[SourceRef(path=source_path)],
                    parser_status="ok",
                    coverage_status=(
                        "complete" if inventory.surface_status == "confirmed" else "unknown"
                    ),
                    evidence_strength="static_proof",
                )
            )
        if "android_native" in inventory.detected_surfaces:
            manifest_path = next(
                (
                    path
                    for signal in inventory.detection_signals
                    for path in [signal.path]
                    if signal.signal_type == "android_manifest" and path
                ),
                "app/src/main/AndroidManifest.xml",
            )
            result = ManifestCollector().collect(sandbox, manifest_path=manifest_path)
            result, namespaced = _namespace_result(result, inventory)
            collector_results.append(result.model_dump(mode="json"))
            facts.extend(namespaced)
        if inventory.detected_surfaces:
            result = DependencyCollector().collect(
                sandbox,
                source_surface=primary or inventory.detected_surfaces[0],
            )
            result, namespaced = _namespace_result(result, inventory)
            collector_results.append(result.model_dump(mode="json"))
            facts.extend(namespaced)
        if "backend_api_doc" in inventory.detected_surfaces:
            result = ApiDocumentCollector().collect(sandbox)
            result, namespaced = _namespace_result(result, inventory)
            collector_results.append(result.model_dump(mode="json"))
            facts.extend(namespaced)
    for material_index, material in enumerate(materials, start=1):
        if material.surface != "backend_api_doc":
            continue
        material_path = Path(material.path).expanduser().resolve()
        if not material_path.exists():
            continue
        sandbox_root = material_path.parent if material_path.is_file() else material_path
        roots = (".",)
        file_globs = (
            (material_path.name,)
            if material_path.is_file()
            else ("*.json", "*.yaml", "*.yml")
        )
        result = ApiDocumentCollector().collect(
            RepositorySandbox(sandbox_root), roots=roots, file_globs=file_globs
        )
        local_facts: list[Fact] = []
        for fact in result.facts:
            source_refs = [
                ref.model_copy(
                    update={
                        "path": (
                            (sandbox_root / ref.path).resolve().as_posix()
                            if ref.path and not ref.path.startswith("/")
                            else ref.path
                        )
                    }
                )
                for ref in fact.source_refs
            ]
            local_facts.append(
                fact.model_copy(
                    update={
                        "fact_id": (
                            f"fact.workspace.material_api_document.{material_index}."
                            f"{fact.fact_id.removeprefix('fact.')}"
                        ),
                        "repo_id": "workspace",
                        "source_refs": source_refs,
                        "limitations": [*fact.limitations, *material.limitations],
                    }
                )
            )
        result = result.model_copy(
            update={
                "repo_id": "workspace",
                "facts": local_facts,
                "limitations": [*result.limitations, *material.limitations],
                "metadata": {
                    **result.metadata,
                    "material_path": material_path.as_posix(),
                    "provenance": material.provenance,
                },
            }
        )
        collector_results.append(result.model_dump(mode="json"))
        facts.extend(local_facts)
    return AppFactSet(
        facts=facts,
        inventory_ids=[inventory.repo_id for inventory in inventories],
        collector_results=collector_results,
    )


def _namespace_result(
    result: CollectorResult, inventory: RepositoryInventory
) -> tuple[CollectorResult, list[Fact]]:
    """Attach repository identity to every Collector result and Fact."""
    collector_result = result
    facts = []
    for fact in collector_result.facts:
        local_id = fact.fact_id.removeprefix("fact.")
        source_refs = [
            ref.model_copy(
                update={
                    "path": (
                        f"{inventory.path}/{ref.path.lstrip('/')}"
                        if ref.path and not ref.path.startswith("/")
                        else ref.path
                    )
                }
            )
            for ref in fact.source_refs
        ]
        facts.append(
            fact.model_copy(
                update={
                    "fact_id": (
                        f"fact.{inventory.repo_id}.{collector_result.collector_id}.{local_id}"
                    ),
                    "repo_id": inventory.repo_id,
                    "source_refs": source_refs,
                }
            )
        )
    return (
        collector_result.model_copy(
            update={"repo_id": inventory.repo_id, "facts": facts}
        ),
        facts,
    )
