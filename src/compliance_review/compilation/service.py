from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from compliance_review.compilation.llm import ControlCompiler, ObligationExtractor
from compliance_review.compilation.models import (
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
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.provider = provider
        self.store = ArtifactStore(self.workspace_root)
        self.source_builder = source_builder or SourceRegistryBuilder()
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

        obligations = self.obligation_extractor.extract(_source_payload(registry))
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


def _source_payload(registry: SourceRegistry) -> dict[str, object]:
    return {
        "contract": registry.contract,
        "version": registry.version,
        "sources": [
            {
                "source_id": source.source_id,
                "title": source.title,
                "version": source.version,
                "source_family": source.source_family,
                "media_type": source.media_type,
                "sections": [section.model_dump(mode="json") for section in source.sections],
            }
            for source in registry.sources
        ],
    }
