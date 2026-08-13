from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

Surface = Literal[
    "frontend_h5",
    "android_native",
    "backend_api_doc",
    "backend_code",
    "play_console",
    "regulator_external",
    "other_external",
]
EvidenceStrength = Literal[
    "declared",
    "static_proof",
    "behavioral_hint",
    "server_doc",
    "server_code",
    "runtime_proof",
]
Severity = Literal["critical", "high", "medium", "low"]
EvidenceStatus = Literal["complete", "partial", "missing", "manual_required"]
ControlStatus = Literal["pass", "fail", "indeterminate", "not_applicable", "waived"]
ExecutionStatus = Literal["pending", "running", "completed", "failed"]
ParserStatus = Literal["ok", "fallback", "failed"]
CoverageStatus = Literal["complete", "partial", "unknown"]
Confidence = Literal["high", "medium", "low"]
WriteRisk = Literal["none", "possible", "high"]
ReviewMode = Literal["full", "diff"]
RunStatus = Literal["pending", "running", "completed", "failed"]
ApplicabilityDecisionStatus = Literal["true", "false", "unknown"]
CoverageUnitStatus = Literal["planned", "missing_surface", "unknown_applicability"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceRef(ContractModel):
    source_id: Optional[str] = Field(default=None, min_length=1)
    source_section: Optional[str] = Field(default=None, min_length=1)
    path: Optional[str] = None
    url: Optional[str] = None
    artifact_path: Optional[str] = None
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    snippet_id: Optional[str] = None

    @field_validator("end_line")
    @classmethod
    def end_line_not_before_start(cls, value: Optional[int], info: Any) -> Optional[int]:
        start_line = info.data.get("start_line")
        if value is not None and start_line is not None and value < start_line:
            raise ValueError("end_line must not be before start_line")
        return value


class EvidenceRequirement(ContractModel):
    minimum_strength: EvidenceStrength
    rationale: str = Field(min_length=1)


class Control(ContractModel):
    control_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    module_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    severity: Severity
    applicability_expression: str = Field(min_length=1)
    required_surfaces: list[Surface] = Field(min_length=1)
    minimum_evidence_strength: dict[Surface, EvidenceStrength]
    missing_evidence_policy: Literal["warn", "block"]
    source_refs: list[SourceRef] = Field(min_length=1)
    reuse_invalidation_keys: list[str] = Field(min_length=1)
    obligation_ids: list[str] = Field(default_factory=list)
    evidence_requirements: dict[Surface, EvidenceRequirement] = Field(default_factory=dict)

    @field_validator("required_surfaces")
    @classmethod
    def unique_surfaces(cls, value: list[Surface]) -> list[Surface]:
        if len(value) != len(set(value)):
            raise ValueError("required_surfaces must not contain duplicates")
        return value

    @field_validator("minimum_evidence_strength")
    @classmethod
    def evidence_strength_covers_surfaces(
        cls, value: dict[Surface, EvidenceStrength], info: Any
    ) -> dict[Surface, EvidenceStrength]:
        required_surfaces = info.data.get("required_surfaces", [])
        missing = set(required_surfaces) - set(value)
        if missing:
            raise ValueError(f"minimum_evidence_strength missing surfaces: {sorted(missing)}")
        return value


class ControlSet(ContractModel):
    contract: Literal["control_set.v1"]
    version: str = Field(min_length=1)
    controls: list[Control] = Field(min_length=1)

    @field_validator("controls")
    @classmethod
    def unique_control_ids(cls, value: list[Control]) -> list[Control]:
        ids = [control.control_id for control in value]
        if len(ids) != len(set(ids)):
            raise ValueError("control_id values must be unique")
        return value


class ApplicabilityProfile(ContractModel):
    contract: Literal["applicability_profile.v1"]
    version: str = Field(min_length=1)
    app_name: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    business_type: list[str] = Field(min_length=1)
    self_lending: Union[bool, Literal["unknown"]]
    evidence_surfaces: list[Surface] = Field(min_length=1)
    review_scope: Literal["full_release_package", "multi_surface_static_review", "partial"]
    roots: dict[Surface, str] = Field(default_factory=dict)


class ApplicabilityDecision(ContractModel):
    control_id: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    status: ApplicabilityDecisionStatus
    reason: str = Field(min_length=1)


class ApplicabilitySet(ContractModel):
    contract: Literal["applicability_set.v1"] = "applicability_set.v1"
    profile_version: str = Field(min_length=1)
    control_version: str = Field(min_length=1)
    decisions: list[ApplicabilityDecision] = Field(default_factory=list)
    excluded_control_ids: list[str] = Field(default_factory=list)
    unknown_control_ids: list[str] = Field(default_factory=list)


class CoverageUnit(ContractModel):
    coverage_unit_id: str = Field(pattern=r"^cu\.[A-Za-z0-9_.-]+$")
    control_id: str = Field(min_length=1)
    module_id: str = Field(min_length=1)
    surface: Surface
    applicability_status: ApplicabilityDecisionStatus
    coverage_status: CoverageUnitStatus
    required_evidence_strength: EvidenceStrength
    reason: str = Field(min_length=1)
    work_item_id: Optional[str] = None


class CoverageSet(ContractModel):
    contract: Literal["coverage_set.v1"] = "coverage_set.v1"
    profile_version: str = Field(min_length=1)
    control_version: str = Field(min_length=1)
    units: list[CoverageUnit] = Field(default_factory=list)
    excluded_control_ids: list[str] = Field(default_factory=list)
    unknown_control_ids: list[str] = Field(default_factory=list)
    missing_surfaces: list[Surface] = Field(default_factory=list)


class Fact(ContractModel):
    fact_id: str = Field(min_length=1)
    repo_id: Optional[str] = Field(default=None, min_length=1)
    source_surface: Surface
    fact_type: str = Field(min_length=1)
    observed_value: Any
    source_refs: list[SourceRef] = Field(min_length=1)
    parser_status: ParserStatus
    coverage_status: CoverageStatus
    evidence_strength: EvidenceStrength
    limitations: list[str] = Field(default_factory=list)


class Evidence(ContractModel):
    evidence_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    control_ids: list[str] = Field(min_length=1)
    source_surface: Surface
    evidence_strength: EvidenceStrength
    sensitive_domains: list[str] = Field(min_length=1)
    write_risk: WriteRisk
    source_kind: Literal[
        "code",
        "config",
        "document",
        "app_ui",
        "store_listing",
        "play_console",
        "backend",
        "manual_input",
    ]
    source_refs: list[SourceRef] = Field(min_length=1)
    summary: str = Field(min_length=1)
    confidence: Confidence
    fact_ids: list[str] = Field(default_factory=list)


class WorkItem(ContractModel):
    work_item_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    module_id: str = Field(min_length=1)
    surface: Surface
    control_ids: list[str] = Field(min_length=1)
    coverage_unit_ids: list[str] = Field(default_factory=list)
    collector_fact_refs: list[str] = Field(default_factory=list)
    allowed_roots: list[str] = Field(default_factory=list)
    target_hints: dict[str, list[str]] = Field(default_factory=dict)
    max_tool_rounds: int = Field(default=12, ge=1)
    max_files_read: int = Field(default=20, ge=1)
    max_lines_per_read: int = Field(default=300, ge=1)


class ControlSurfaceResult(ContractModel):
    control_id: str = Field(min_length=1)
    surface: Surface
    evidence_status: EvidenceStatus
    recommended_control_status: ControlStatus
    evidence_ids: list[str] = Field(default_factory=list)
    gap_reasons: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class ReviewResult(ContractModel):
    contract: Literal["review_result.v1"]
    work_item_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    execution_status: ExecutionStatus
    rows: list[ControlSurfaceResult] = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    verifier_required: bool = False
    errors: list[str] = Field(default_factory=list)


class Snapshot(ContractModel):
    contract: Literal["compliance_snapshot.v1"]
    run_id: str = Field(min_length=1)
    git_revision: str = Field(min_length=1)
    mode: ReviewMode
    baseline_run_id: Optional[str] = None
    control_results: list[ControlSurfaceResult] = Field(default_factory=list)
    reviewed_rows: list[str] = Field(default_factory=list)
    reused_rows: list[str] = Field(default_factory=list)
    missing_surfaces: list[Surface] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    run_status: RunStatus
