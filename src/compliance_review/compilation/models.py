from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from compliance_review.domain.models import (
    ContractModel,
    ControlSet,
    EvidenceRequirement,
    Severity,
    SourceRef,
    Surface,
)

SourceMediaType = Literal["md", "txt", "pdf", "docx"]
CompilationStatus = Literal["draft", "validated"]


class SourceSection(ContractModel):
    section_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    page: Optional[int] = Field(default=None, ge=1)
    page_end: Optional[int] = Field(default=None, ge=1)
    location: Optional[str] = None


class SourceSectionBatch(ContractModel):
    """A bounded model input containing complete sections from one source."""

    batch_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    source_id: str = Field(min_length=1)
    sections: list[SourceSection] = Field(min_length=1)
    estimated_input_tokens: int = Field(ge=1)


SectionDecision = Literal["obligations_extracted", "no_obligation"]


class SectionCoverageDecision(ContractModel):
    section_id: str = Field(min_length=1)
    decision: SectionDecision
    obligation_ids: list[str] = Field(default_factory=list)
    reason: Optional[str] = None


class ComplianceSource(ContractModel):
    contract: Literal["compliance_source.v1"] = "compliance_source.v1"
    source_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    version: Optional[str] = None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_family: str = Field(min_length=1)
    media_type: SourceMediaType
    extraction_status: Literal["ok", "partial", "failed"]
    sections: list[SourceSection] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SourceRegistry(ContractModel):
    contract: Literal["source_registry.v1"] = "source_registry.v1"
    version: str = Field(min_length=1)
    sources: list[ComplianceSource] = Field(min_length=1)


class Obligation(ContractModel):
    obligation_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    source_id: str = Field(min_length=1)
    source_section: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    concepts: list[str] = Field(min_length=1)
    applicability_expression: str = Field(min_length=1)
    required_surfaces: list[Surface] = Field(min_length=1)
    source_refs: list[SourceRef] = Field(min_length=1)


class ObligationExtractionBatchResult(ContractModel):
    contract: Literal["obligation_extraction_batch.v1"] = "obligation_extraction_batch.v1"
    version: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    section_decisions: list[SectionCoverageDecision] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)


class ObligationSet(ContractModel):
    contract: Literal["obligation_set.v1"] = "obligation_set.v1"
    version: str = Field(min_length=1)
    status: CompilationStatus
    obligations: list[Obligation] = Field(default_factory=list)


class ControlDraft(ContractModel):
    control_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    module_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=1)
    severity: Severity
    obligation_ids: list[str] = Field(min_length=1)
    source_refs: list[SourceRef] = Field(min_length=1)
    applicability_expression: str = Field(min_length=1)
    required_surfaces: list[Surface] = Field(min_length=1)
    evidence_requirements: dict[Surface, EvidenceRequirement] = Field(min_length=1)
    missing_evidence_policy: Literal["warn", "block"]
    reuse_invalidation_keys: list[str] = Field(min_length=1)


class ControlDraftSet(ContractModel):
    contract: Literal["control_draft_set.v1"] = "control_draft_set.v1"
    version: str = Field(min_length=1)
    status: Literal["draft"] = "draft"
    controls: list[ControlDraft] = Field(default_factory=list)


class ControlValidationResult(ContractModel):
    contract: Literal["control_validation.v1"] = "control_validation.v1"
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duplicate_obligation_ids: list[str] = Field(default_factory=list)
    duplicate_control_ids: list[str] = Field(default_factory=list)
    duplicate_control_groups: list[str] = Field(default_factory=list)
    validated_control_count: int = Field(default=0, ge=0)


class Phase2CompilationResult(ContractModel):
    source_registry: SourceRegistry
    extraction_batches: list[ObligationExtractionBatchResult] = Field(default_factory=list)
    obligations: ObligationSet
    controls_draft: ControlDraftSet
    control_validation: ControlValidationResult
    controls: Optional[ControlSet] = None
