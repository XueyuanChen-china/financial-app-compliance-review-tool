from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field, model_validator

from compliance_review.domain.models import (
    ApplicabilityCondition,
    ContractModel,
    ControlSet,
    EvidenceRequirement,
    EvidenceStrength,
    Severity,
    SourceRef,
    Surface,
    parse_legacy_applicability_expression,
    render_legacy_applicability_condition,
)

SourceMediaType = Literal["md", "txt", "pdf", "docx"]
CompilationStatus = Literal["draft", "validated"]

_APPLICABILITY_FIELD = r"(?:business_type|evidence_surfaces|self_lending|jurisdiction)"
_APPLICABILITY_VALUE = r"[A-Za-z0-9_.-]+"
_APPLICABILITY_CLAUSE = (
    rf"(?:{_APPLICABILITY_FIELD}\s*(?:==\s*{_APPLICABILITY_VALUE}"
    rf"|includes\s+{_APPLICABILITY_VALUE})"
    rf"|{_APPLICABILITY_VALUE}\s+in\s+{_APPLICABILITY_FIELD})"
)
APPLICABILITY_EXPRESSION_PATTERN = (
    rf"^(?:unknown|{_APPLICABILITY_CLAUSE}"
    rf"(?:\s+(?:and|&&)\s+{_APPLICABILITY_CLAUSE})*)$"
)


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
    applicability_condition: ApplicabilityCondition
    required_surfaces: list[Surface] = Field(min_length=1)
    source_refs: list[SourceRef] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_condition(cls, value: Any) -> Any:
        if isinstance(value, dict) and "applicability_condition" not in value:
            value = dict(value)
            expression = value.pop("applicability_expression", None)
            if expression is not None:
                value["applicability_condition"] = parse_legacy_applicability_expression(expression)
        return value

    @property
    def applicability_expression(self) -> str:
        return render_legacy_applicability_condition(self.applicability_condition)


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
    applicability_condition: ApplicabilityCondition
    candidate_surfaces: list[Surface] = Field(default_factory=list)
    required_surfaces: list[Surface] = Field(default_factory=list)
    evidence_requirements: dict[Surface, EvidenceRequirement] = Field(min_length=1)
    missing_evidence_policy: Literal["warn", "block"]
    reuse_invalidation_keys: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_condition(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            if not value.get("candidate_surfaces") and value.get("required_surfaces"):
                value["candidate_surfaces"] = list(value["required_surfaces"])
            if not value.get("required_surfaces") and value.get("candidate_surfaces"):
                value["required_surfaces"] = list(value["candidate_surfaces"])
        if isinstance(value, dict) and "applicability_condition" not in value:
            expression = value.pop("applicability_expression", None)
            if expression is not None:
                value["applicability_condition"] = parse_legacy_applicability_expression(expression)
        return value

    @model_validator(mode="after")
    def validate_surface_contract(self) -> "ControlDraft":
        if not self.candidate_surfaces:
            raise ValueError("ControlDraft must declare at least one candidate surface")
        return self

    @property
    def surface_candidates(self) -> list[Surface]:
        if self.required_surfaces and self.required_surfaces != self.candidate_surfaces:
            return list(self.required_surfaces)
        return list(self.candidate_surfaces or self.required_surfaces)

    @property
    def applicability_expression(self) -> str:
        return render_legacy_applicability_condition(self.applicability_condition)


class ControlDraftSet(ContractModel):
    contract: Literal["control_draft_set.v1"] = "control_draft_set.v1"
    version: str = Field(min_length=1)
    status: Literal["draft"] = "draft"
    controls: list[ControlDraft] = Field(default_factory=list)


class ControlEvidenceRequirementItem(ContractModel):
    """Transport-friendly form of a surface-keyed evidence requirement."""

    surface: Surface
    minimum_strength: EvidenceStrength
    rationale: str = Field(min_length=1)
    condition: Optional[ApplicabilityCondition] = None


class ControlDraftTransport(ContractModel):
    """Structured-output shape that avoids arbitrary JSON object property names."""

    control_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    module_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=1)
    severity: Literal["critical", "high", "medium", "low"]
    obligation_ids: list[str] = Field(min_length=1)
    evidence_requirements: list[ControlEvidenceRequirementItem] = Field(min_length=1)
    missing_evidence_policy: Literal["warn", "block"]
    reuse_invalidation_keys: list[str] = Field(min_length=1)


class ControlDraftSetTransport(ContractModel):
    """Transport shape for providers that reject map schemas in strict mode."""

    contract: Literal["control_draft_set.v1"] = "control_draft_set.v1"
    version: str = Field(min_length=1)
    status: Literal["draft"] = "draft"
    controls: list[ControlDraftTransport] = Field(default_factory=list)


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
