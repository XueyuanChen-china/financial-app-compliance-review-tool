from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from compliance_review.compilation.batching import (
    BatchPlanner,
    merge_obligation_batches,
    validate_batch_coverage,
    validate_registry_coverage,
)
from compliance_review.compilation.llm import ControlCompiler, ObligationExtractor
from compliance_review.compilation.models import (
    ObligationSet,
    Phase2CompilationResult,
    SourceRegistry,
)
from compliance_review.compilation.source_registry import SourceRegistryBuilder
from compliance_review.compilation.validator import ControlValidator
from compliance_review.persistence import ArtifactStore
from compliance_review.review.provider import ModelProvider


class Phase2CompilationError(ValueError):
    """Raised when Phase 2 cannot produce a validated ControlSet."""


class Phase2CompilationService:
    """Compile source materials through source -> obligation -> control stages."""

    def __init__(
        self,
        workspace_root: Path,
        provider: ModelProvider,
        source_builder: Optional[SourceRegistryBuilder] = None,
        batch_planner: Optional[BatchPlanner] = None,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.provider = provider
        self.store = ArtifactStore(self.workspace_root)
        self.source_builder = source_builder or SourceRegistryBuilder()
        self.batch_planner = batch_planner or BatchPlanner()
        self.validator = ControlValidator()
        self.obligation_extractor = ObligationExtractor(provider)
        self.control_compiler = ControlCompiler(provider)

    def compile(
        self,
        paths: Iterable[Path],
        source_families: Optional[dict[str, str]] = None,
        versions: Optional[dict[str, str]] = None,
    ) -> Phase2CompilationResult:
        # A failed current compilation must not leave an old validated artifact
        # looking like the result of this run.
        self.store.invalidate_controls()
        registry = self.source_builder.build(paths, source_families, versions)
        self.store.write_source_registry(registry)
        self._ensure_extractable(registry)

        try:
            batches = self.batch_planner.plan(registry)
        except ValueError as exc:
            raise Phase2CompilationError(f"source batching failed: {exc}") from exc
        extraction_results = []
        for batch in batches:
            extraction = self.obligation_extractor.extract(batch)
            try:
                validate_batch_coverage(batch, extraction)
            except ValueError as exc:
                raise Phase2CompilationError(
                    f"obligation extraction coverage failed for {batch.batch_id}: {exc}"
                ) from exc
            extraction_results.append(extraction)
        try:
            validate_registry_coverage(registry, extraction_results)
            merged = merge_obligation_batches(extraction_results)
        except ValueError as exc:
            raise Phase2CompilationError(f"obligation extraction coverage failed: {exc}") from exc
        obligations = ObligationSet(
            version="1.0",
            status="draft",
            obligations=merged,
        )
        self.store.write_obligation_extraction_batches(extraction_results)
        self.store.write_obligations(obligations)

        drafts = self.control_compiler.compile(
            {
                "contract": "obligation_set.v1",
                "version": obligations.version,
                "obligations": [item.model_dump(mode="json") for item in obligations.obligations],
            },
        )
        self.store.write_controls_draft(drafts)
        validation = self.validator.validate(registry, obligations, drafts)
        self.store.write_control_validation(validation)
        if not validation.valid:
            raise Phase2CompilationError(
                "control compilation failed deterministic validation: "
                + "; ".join(validation.errors)
            )
        controls = self.validator.to_control_set(drafts, validation)
        self.store.write_controls(controls)
        return Phase2CompilationResult(
            source_registry=registry,
            extraction_batches=extraction_results,
            obligations=obligations,
            controls_draft=drafts,
            control_validation=validation,
            controls=controls,
        )

    @staticmethod
    def _ensure_extractable(registry: SourceRegistry) -> None:
        failed = [source.source_id for source in registry.sources if not source.sections]
        if failed:
            raise Phase2CompilationError(
                "source extraction failed or produced no text for: " + ", ".join(failed)
            )
