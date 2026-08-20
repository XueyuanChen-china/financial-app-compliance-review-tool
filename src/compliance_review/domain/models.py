from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Surface = Literal[
    "frontend_h5",
    "android_native",
    "backend_api_doc",
    "backend_code",
    "play_console",
    "regulator_external",
    "other_external",
]
ExternalEvidencePolicy = Literal["strict", "trusted_test_materials"]
EvidenceStrength = Literal[
    "declared",
    "static_proof",
    "behavioral_hint",
    "server_doc",
    "server_code",
    "runtime_proof",
]
Severity = Literal["critical", "high", "medium", "low"]
ReviewerEvidenceStatus = Literal["complete", "partial", "missing", "manual_required"]
CoverageEvidenceStatus = Literal[
    "complete", "partial", "missing", "manual_required", "not_required", "not_applicable"
]
ControlStatus = Literal["pass", "fail", "indeterminate", "not_applicable", "waived"]
ExecutionStatus = Literal["pending", "running", "completed", "failed"]
CoverageExecutionStatus = Literal[
    "pending", "running", "completed", "failed", "not_required", "manual_required"
]
ParserStatus = Literal["ok", "fallback", "failed"]
CoverageStatus = Literal["complete", "partial", "unknown"]
Confidence = Literal["high", "medium", "low"]
WriteRisk = Literal["none", "possible", "high"]
ReviewMode = Literal["full", "diff"]
RunStatus = Literal["pending", "running", "completed", "failed"]
CiStatus = Literal["pass", "warn", "block"]
ChangeType = Literal["add", "modify", "delete", "rename"]
ResultOrigin = Literal[
    "reviewed",
    "carried_forward",
    "reused",
    "manual_required",
    "blocked",
    "not_applicable",
    "not_required",
    "waived",
]
ApplicabilityDecisionStatus = Literal["applicable", "not_applicable", "unknown"]
SurfaceRequirementStatus = Literal["required", "not_required", "unknown"]
DiscoveryTerminalStatus = Literal["resolved", "manual_required", "failed_exhausted"]
DiscoveryFactStatus = Literal["candidate", "verified", "unresolved"]
CoverageUnitStatus = Literal[
    "planned",
    "missing_surface",
    "unknown_applicability",
    "not_applicable",
    "not_required",
    "manual_required",
    "external_collection_required",
]


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


class ApplicabilityCondition(ContractModel):
    """Structured, finite applicability condition used by new rule artifacts.

    The condition is deliberately data-only.  It is not an expression language
    and is never evaluated with ``eval``.  ``unknown`` is used by migration and
    compilation when the source semantics cannot be represented safely.
    """

    kind: Literal["atom", "all_of", "any_of", "unknown"]
    fact: Optional[str] = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    operator: Optional[Literal["equals", "includes"]] = None
    value: Any = None
    conditions: list["ApplicabilityCondition"] = Field(default_factory=list)
    reason: Optional[str] = None

    @model_validator(mode="after")
    def validate_shape(self) -> "ApplicabilityCondition":
        if self.kind == "atom":
            if not self.fact or self.operator is None:
                raise ValueError("atom applicability condition requires fact and operator")
            if self.value is None:
                raise ValueError("atom applicability condition requires value")
            if self.conditions or self.reason:
                raise ValueError("atom applicability condition cannot contain children/reason")
        elif self.kind in {"all_of", "any_of"}:
            if len(self.conditions) < 1:
                raise ValueError(f"{self.kind} applicability condition requires children")
            if self.fact or self.operator or self.value is not None or self.reason:
                raise ValueError(f"{self.kind} applicability condition has invalid atom fields")
        elif self.kind == "unknown":
            if self.fact or self.operator or self.value is not None or self.conditions:
                raise ValueError("unknown applicability condition cannot contain executable fields")
        return self

    @classmethod
    def unknown(cls, reason: str = "condition is unresolved") -> "ApplicabilityCondition":
        return cls(kind="unknown", reason=reason)


def parse_legacy_applicability_expression(expression: str) -> ApplicabilityCondition | None:
    """Read the old v1 ``and``-only hint without making it a new DSL.

    This adapter exists only for old checked-in artifacts and test fixtures.  A
    lossy expression, including one using ``or``/``||``, is intentionally
    returned as ``unknown`` rather than being guessed.
    """

    import re

    text = expression.strip()
    if not text or text.lower() == "unknown":
        return ApplicabilityCondition.unknown("legacy applicability was unknown")
    if re.search(r"\s+(?:or|\|\|)\s+", text, flags=re.IGNORECASE):
        return ApplicabilityCondition.unknown("legacy OR expression requires manual migration")
    clauses = re.split(r"\s+(?:and|&&)\s+", text, flags=re.IGNORECASE)
    children: list[ApplicabilityCondition] = []
    for clause in clauses:
        match = re.match(
            r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<value>.+)$", clause.strip()
        )
        operator: Literal["equals", "includes"] = "equals"
        if match is None:
            match = re.match(
                r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+includes\s+(?P<value>.+)$",
                clause.strip(),
            )
            operator = "includes"
        if match is None:
            match = re.match(
                r"^(?P<value>[A-Za-z0-9_.-]+)\s+in\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)$",
                clause.strip(),
            )
            operator = "includes"
        if match is None:
            return ApplicabilityCondition.unknown(
                f"legacy applicability clause is not safely representable: {clause.strip()}"
            )
        raw_value = match.group("value").strip()
        if raw_value.lower() == "true":
            value: Any = True
        elif raw_value.lower() == "false":
            value = False
        elif raw_value.lower() == "null":
            value = None
        else:
            value = raw_value
        children.append(
            ApplicabilityCondition(
                kind="atom",
                fact=match.group("field"),
                operator=operator,
                value=value,
            )
        )
    if len(children) == 1:
        return children[0]
    return ApplicabilityCondition(kind="all_of", conditions=children)


def render_legacy_applicability_condition(condition: ApplicabilityCondition) -> str:
    """Render only for legacy prompt/context compatibility; new files omit it."""

    if condition.kind == "unknown":
        return "unknown"
    if condition.kind == "atom":
        value = (
            str(condition.value).lower()
            if isinstance(condition.value, bool)
            else str(condition.value)
        )
        if condition.operator == "includes":
            return f"{condition.fact} includes {value}"
        return f"{condition.fact} == {value}"
    joiner = " and " if condition.kind == "all_of" else " or "
    return joiner.join(
        render_legacy_applicability_condition(child) for child in condition.conditions
    )


class EvidenceRequirement(ContractModel):
    requirement_id: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    minimum_strength: EvidenceStrength
    rationale: str = Field(min_length=1)
    obligation_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    condition: Optional[ApplicabilityCondition] = None


class Control(ContractModel):
    control_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    module_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=1)
    severity: Severity
    applicability_condition: ApplicabilityCondition
    # Policy compilation produces candidate surfaces. Applicability resolves
    # the final required surfaces for the current application.
    candidate_surfaces: list[Surface] = Field(default_factory=list)
    # Compatibility field for v1 artifacts and callers.
    required_surfaces: list[Surface] = Field(default_factory=list)
    minimum_evidence_strength: dict[Surface, EvidenceStrength]
    missing_evidence_policy: Literal["warn", "block"]
    source_refs: list[SourceRef] = Field(min_length=1)
    reuse_invalidation_keys: list[str] = Field(min_length=1)
    obligation_ids: list[str] = Field(default_factory=list)
    evidence_requirements: dict[Surface, EvidenceRequirement] = Field(default_factory=dict)

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
    def validate_surface_contract(self) -> "Control":
        if not self.candidate_surfaces:
            raise ValueError("Control must declare at least one candidate surface")
        return self

    @property
    def surface_candidates(self) -> list[Surface]:
        # ``model_copy(update={"required_surfaces": ...})`` is common in v1
        # callers and does not rerun the migration validator. Prefer that
        # explicit legacy update when the two compatibility fields diverge.
        if self.required_surfaces and self.required_surfaces != self.candidate_surfaces:
            return list(self.required_surfaces)
        return list(self.candidate_surfaces or self.required_surfaces)

    @property
    def applicability_expression(self) -> str:
        return render_legacy_applicability_condition(self.applicability_condition)

    @field_validator("candidate_surfaces")
    @classmethod
    def unique_surfaces(cls, value: list[Surface]) -> list[Surface]:
        if len(value) != len(set(value)):
            raise ValueError("candidate_surfaces must not contain duplicates")
        return value

    @field_validator("minimum_evidence_strength")
    @classmethod
    def evidence_strength_covers_surfaces(
        cls, value: dict[Surface, EvidenceStrength], info: Any
    ) -> dict[Surface, EvidenceStrength]:
        candidate_surfaces = info.data.get("candidate_surfaces", [])
        missing = set(candidate_surfaces) - set(value)
        if missing:
            raise ValueError(
                "minimum_evidence_strength missing candidate surfaces: "
                f"{sorted(missing)}"
            )
        return value


class ControlSet(ContractModel):
    contract: Literal["control_set.v1", "control_set.v2"]
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
    contract: Literal["applicability_profile.v1", "applicability_profile.v2"]
    version: str = Field(min_length=1)
    app_name: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    business_type: list[str] = Field(min_length=1)
    self_lending: Union[bool, Literal["unknown"]]
    evidence_surfaces: list[Surface] = Field(min_length=1)
    review_scope: Literal["full_release_package", "multi_surface_static_review", "partial"]
    roots: dict[Surface, str] = Field(default_factory=dict)
    confirmed_facts: dict[str, "ApplicabilityProfileFact"] = Field(default_factory=dict)


class ApplicabilityProfileFact(ContractModel):
    """A profile value that an applicability decision may cite."""

    value: Any
    source: Literal["declared", "human_confirmed", "deterministic", "inferred", "unresolved"]


class ProfileFactRef(ContractModel):
    field_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # JSON text retains arbitrary profile values while presenting a concrete type
    # to strict OpenAI-compatible schema providers.
    expected_value: str = Field(min_length=1)


class SurfaceRequirementDecision(ContractModel):
    surface: Surface
    decision: SurfaceRequirementStatus
    reason: str = Field(min_length=1)
    source_refs: list[SourceRef] = Field(default_factory=list)
    profile_fact_refs: list[ProfileFactRef] = Field(default_factory=list)


class ApplicabilityDecision(ContractModel):
    control_id: str = Field(min_length=1)
    decision: ApplicabilityDecisionStatus
    reason: str = Field(min_length=1)
    source_refs: list[SourceRef] = Field(default_factory=list)
    profile_fact_refs: list[ProfileFactRef] = Field(default_factory=list)
    technical_fact_refs: list[str] = Field(default_factory=list)
    surface_requirements: list[SurfaceRequirementDecision] = Field(default_factory=list)
    resolved_required_surfaces: list[Surface] = Field(default_factory=list)
    unresolved_conditions: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"


class ApplicabilitySet(ContractModel):
    contract: Literal["applicability_set.v1", "applicability_set.v2"] = "applicability_set.v2"
    profile_version: str = Field(min_length=1)
    control_version: str = Field(min_length=1)
    decisions: list[ApplicabilityDecision] = Field(default_factory=list)
    excluded_control_ids: list[str] = Field(default_factory=list)
    unknown_control_ids: list[str] = Field(default_factory=list)


class ApplicabilityQuestion(ContractModel):
    """One deduplicated human fact question produced by the resolution loop."""

    question_id: str = Field(pattern=r"^aq\.[A-Za-z0-9_.-]+$")
    fact_key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    question: str = Field(min_length=1)
    affected_control_ids: list[str] = Field(min_length=1)
    status: Literal["pending", "answered"] = "pending"
    answer: Any = None


class ApplicabilityValidationIssue(ContractModel):
    """Machine-readable rejection reason for one Applicability draft."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    control_id: Optional[str] = None
    field: Optional[str] = None


class ApplicabilityResolution(ContractModel):
    """Durable output of the bounded Applicability Agent Loop."""

    contract: Literal["applicability_resolution.v1"] = "applicability_resolution.v1"
    profile_version: str = Field(min_length=1)
    control_version: str = Field(min_length=1)
    status: Literal["complete", "awaiting_human", "partial"]
    decisions: list[ApplicabilityDecision] = Field(default_factory=list)
    pending_questions: list[ApplicabilityQuestion] = Field(default_factory=list)
    validation_issues: list[ApplicabilityValidationIssue] = Field(default_factory=list)
    attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class ApplicabilityAnswerSet(ContractModel):
    """Human applicability answers bound to the profile and Control snapshot."""

    contract: Literal["applicability_answers.v1"] = "applicability_answers.v1"
    profile_fingerprint: str = Field(min_length=1)
    control_fingerprint: str = Field(min_length=1)
    answers: dict[str, Any] = Field(default_factory=dict)


class ApplicabilityResolutionCheckpoint(ContractModel):
    """Durable per-Control progress for resumable applicability resolution."""

    contract: Literal["applicability_resolution_checkpoint.v1"] = (
        "applicability_resolution_checkpoint.v1"
    )
    profile_fingerprint: str = Field(min_length=1)
    control_fingerprint: str = Field(min_length=1)
    decisions: list[ApplicabilityDecision] = Field(default_factory=list)
    attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class ApplicabilityDiscoveryWorkItem(ContractModel):
    """Bounded technical fact discovery; it cannot emit compliance decisions."""

    work_item_type: Literal["applicability_discovery"] = "applicability_discovery"
    discovery_id: str = Field(pattern=r"^adw\.[A-Za-z0-9_.-]+$")
    unresolved_fact_keys: list[str] = Field(min_length=1)
    dependent_control_ids: list[str] = Field(min_length=1)
    allowed_surfaces: list[Surface] = Field(min_length=1)
    allowed_roots: dict[Surface, list[str]] = Field(default_factory=dict)
    max_tool_rounds: int = Field(default=6, ge=1)
    max_files_read: int = Field(default=12, ge=1)
    preparation_version: str = Field(min_length=1)
    terminal_status: Optional[DiscoveryTerminalStatus] = None


class ApplicabilityDiscoveryPlan(ContractModel):
    contract: Literal["applicability_discovery.v1"] = "applicability_discovery.v1"
    preparation_version: str = Field(min_length=1)
    work_items: list[ApplicabilityDiscoveryWorkItem] = Field(default_factory=list)
    terminal_gaps: dict[str, list[str]] = Field(default_factory=dict)


class DiscoveredProfileFact(ContractModel):
    """A bounded technical fact; it carries no compliance conclusion."""

    fact_key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: Any
    status: DiscoveryFactStatus
    source_surface: Surface
    source_fact_ids: list[str] = Field(default_factory=list)
    anchor_ids: list[str] = Field(default_factory=list)
    validator_outcome: Literal["accepted", "candidate_only", "unresolved"]
    limitations: list[str] = Field(default_factory=list)


class ApplicabilityDiscoveryResult(ContractModel):
    """Terminal result for one discovery queue item."""

    discovery_id: str = Field(pattern=r"^adw\.[A-Za-z0-9_.-]+$")
    terminal_status: DiscoveryTerminalStatus
    facts: list[DiscoveredProfileFact] = Field(default_factory=list)
    dependent_control_ids: list[str] = Field(min_length=1)
    reasons: list[str] = Field(default_factory=list)


class ApplicabilityDiscoveryResultSet(ContractModel):
    contract: Literal["applicability_discovery_results.v1"] = (
        "applicability_discovery_results.v1"
    )
    preparation_version: str = Field(min_length=1)
    results: list[ApplicabilityDiscoveryResult] = Field(default_factory=list)
    terminal_gaps: dict[str, list[str]] = Field(default_factory=dict)
    barrier_complete: bool = False


class CoverageUnit(ContractModel):
    coverage_unit_id: str = Field(pattern=r"^cu\.[A-Za-z0-9_.-]+$")
    control_id: str = Field(min_length=1)
    module_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    surface: Surface
    applicability_status: ApplicabilityDecisionStatus
    coverage_status: CoverageUnitStatus
    required_evidence_strength: EvidenceStrength
    reason: str = Field(min_length=1)
    evidence_requirement_rationale: str = Field(
        default="No evidence requirement rationale recorded.", min_length=1
    )
    obligation_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    evidence_requirement_ids: list[str] = Field(default_factory=list)
    work_item_id: Optional[str] = None


class CoverageSet(ContractModel):
    contract: Literal["coverage_set.v1", "coverage_set.v2"] = "coverage_set.v2"
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


class EvidenceAnchor(ContractModel):
    anchor_id: str = Field(min_length=1)
    # Anchors are repository facts, not control-owned claims.  Control
    # citations are carried by the row/fact relationship instead.
    control_ids: list[str] = Field(default_factory=list)
    repository_id: str = Field(default="workspace", pattern=r"^[A-Za-z0-9_.-]+$")
    source_surface: Surface
    source_tool: str = Field(min_length=1)
    path: Optional[str] = None
    symbol: Optional[str] = None
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    exact_snippet: Optional[str] = None
    normalized_snippet_hash: Optional[str] = None
    file_revision: Optional[str] = None
    evidence_strength: EvidenceStrength
    fact_ids: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)


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
    work_item_type: Literal[
        "compliance_review",
        "applicability_discovery",
        "applicability_resolution",
        "semantic_applicability",
        "compilation",
        "profile_discovery",
        "verification",
    ] = "compliance_review"
    mode: ReviewMode = "full"
    work_item_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    module_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repository_id: str = Field(default="workspace", pattern=r"^[A-Za-z0-9_.-]+$")
    repository_ids: list[str] = Field(default_factory=list)
    surface: Surface
    external_evidence_policy: ExternalEvidencePolicy = "strict"
    control_ids: list[str] = Field(min_length=1)
    coverage_unit_ids: list[str] = Field(default_factory=list)
    collector_fact_refs: list[str] = Field(default_factory=list)
    allowed_roots: list[str] = Field(default_factory=list)
    target_hints: dict[str, list[str]] = Field(default_factory=dict)
    max_tool_rounds: int = Field(default=12, ge=1)
    max_files_read: int = Field(default=20, ge=1)
    max_lines_per_read: int = Field(default=300, ge=1)
    # Diff-specific context is data-only.  The parent process owns its
    # construction, so the Reviewer cannot silently broaden the scope.
    baseline_context: Optional[dict[str, Any]] = None
    change_context: Optional[dict[str, Any]] = None
    evidence_requirement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_work_item_scope(self) -> "WorkItem":
        if self.work_item_type == "compliance_review":
            if len(self.control_ids) != 1:
                raise ValueError(
                    "formal compliance_review WorkItem must bind exactly one control"
                )
            if not self.coverage_unit_ids:
                # Legacy callers created a single-control WorkItem before the
                # coverage ledger existed.  Derive the only unambiguous unit;
                # new planners always persist it explicitly.
                self.coverage_unit_ids = [f"cu.{self.control_ids[0]}.{self.surface}"]
            elif len(self.coverage_unit_ids) != 1:
                raise ValueError(
                    "formal compliance_review WorkItem must bind exactly one coverage unit"
                )
        return self

    @property
    def control_id(self) -> str:
        return self.control_ids[0]

    @property
    def coverage_unit_id(self) -> str:
        return self.coverage_unit_ids[0]


class BaseReviewWorkItem(WorkItem):
    """Stable common contract for full and code-only incremental review."""

    work_item_type: Literal["compliance_review"] = "compliance_review"


class FullReviewWorkItem(BaseReviewWorkItem):
    mode: Literal["full"] = "full"

    @model_validator(mode="after")
    def reject_diff_context(self) -> "FullReviewWorkItem":
        if self.baseline_context is not None or self.change_context is not None:
            raise ValueError("full ReviewWorkItem must not include diff-only context")
        return self


class DiffReviewWorkItem(BaseReviewWorkItem):
    mode: Literal["diff"] = "diff"
    baseline_context: dict[str, Any] = Field(default_factory=dict)
    change_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_diff_context(self) -> "DiffReviewWorkItem":
        if not self.baseline_context:
            raise ValueError("diff ReviewWorkItem requires baseline_context")
        if not self.change_context:
            raise ValueError("diff ReviewWorkItem requires change_context")
        return self


class ControlSurfaceResult(ContractModel):
    control_id: str = Field(min_length=1)
    surface: Surface
    evidence_status: ReviewerEvidenceStatus
    recommended_control_status: ControlStatus
    evidence_ids: list[str] = Field(default_factory=list)
    observed_evidence_strength: Optional[EvidenceStrength] = None
    anchor_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    unsupported_inferences: list[str] = Field(default_factory=list)
    gap_reasons: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    requirement_results: list["EvidenceRequirementResult"] = Field(default_factory=list)


class EvidenceRequirementResult(ContractModel):
    requirement_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    evidence_status: ReviewerEvidenceStatus
    anchor_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    gap_reasons: list[str] = Field(default_factory=list)


class ReviewResult(ContractModel):
    contract: Literal["review_result.v1"]
    work_item_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    execution_status: ExecutionStatus
    rows: list[ControlSurfaceResult] = Field(min_length=1)
    anchors: list[EvidenceAnchor] = Field(default_factory=list)
    agent_id: str = Field(min_length=1)
    verifier_required: bool = False
    errors: list[str] = Field(default_factory=list)


class ResolvedControlResult(ContractModel):
    control_id: str = Field(min_length=1)
    status: ControlStatus
    severity: Severity
    coverage_unit_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    verifier_row_ids: list[str] = Field(default_factory=list)


class CoverageManifestRow(ContractModel):
    coverage_unit_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    surface: Surface
    work_item_id: Optional[str] = None
    attempt_id: Optional[str] = None
    execution_status: CoverageExecutionStatus
    evidence_status: CoverageEvidenceStatus
    result_origin: ResultOrigin
    coverage_reason: str = Field(default="coverage reason unavailable", min_length=1)
    previous_run_id: Optional[str] = None
    # ``previous_run_id`` is the immediate handoff.  The original reviewed run
    # remains stable through repeated carry-forward chains.
    result_origin_run_id: Optional[str] = None
    resolution_status: ControlStatus


class CoverageGateResult(ContractModel):
    contract: Literal["coverage_gate_result.v1"] = "coverage_gate_result.v1"
    complete: bool
    ci_status: CiStatus
    rows: list[CoverageManifestRow] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    warning_reasons: list[str] = Field(default_factory=list)
    validation_flags: dict[str, list[str]] = Field(default_factory=dict)
    manual_review_new_ids: list[str] = Field(default_factory=list)
    manual_review_existing_ids: list[str] = Field(default_factory=list)
    manual_review_resolved_ids: list[str] = Field(default_factory=list)
    automated_evidence_regression_ids: list[str] = Field(default_factory=list)


class Snapshot(ContractModel):
    contract: Literal["compliance_snapshot.v1"]
    run_id: str = Field(min_length=1)
    git_revision: str = Field(min_length=1)
    mode: ReviewMode
    # The immutable Full Review that froze controls, profile, and applicability.
    # Diff runs use baseline_run_id for their immediate predecessor instead.
    semantic_baseline_run_id: Optional[str] = None
    baseline_run_id: Optional[str] = None
    control_results: list[ResolvedControlResult] = Field(default_factory=list)
    coverage_manifest_ref: str = Field(min_length=1)
    applicability_hash: str = Field(min_length=1)
    ci_status: CiStatus
    reviewed_rows: list[str] = Field(default_factory=list)
    reviewed_partial_rows: list[str] = Field(default_factory=list)
    reused_rows: list[str] = Field(default_factory=list)
    reviewer_work_items_completed: int = Field(default=0, ge=0)
    reviewer_work_items_failed: int = Field(default=0, ge=0)
    applicability_decisions: list[ApplicabilityDecision] = Field(default_factory=list)
    missing_surfaces: list[Surface] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    validation_flags: dict[str, list[str]] = Field(default_factory=dict)
    manual_review_new_ids: list[str] = Field(default_factory=list)
    manual_review_existing_ids: list[str] = Field(default_factory=list)
    manual_review_resolved_ids: list[str] = Field(default_factory=list)
    automated_evidence_regression_ids: list[str] = Field(default_factory=list)
    run_status: RunStatus
    repository_revisions: dict[str, str] = Field(default_factory=dict)
    repository_fingerprints: dict[str, str] = Field(default_factory=dict)
    reuse_fingerprints: dict[str, str] = Field(default_factory=dict)
    input_baseline_ref: Optional[str] = None
    code_state_ids: dict[str, str] = Field(default_factory=dict)


class ReviewInputFingerprint(ContractModel):
    artifact_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    category: Literal[
        "controls",
        "obligations",
        "sources",
        "app_profile",
        "api_documents",
        "play_console",
        "regulator_external",
        "other_external",
        "inventory",
        "applicability",
        "workspace",
    ]
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=1)


class InputBaselineComparison(ContractModel):
    full_review_required: bool
    changed_artifact_ids: list[str] = Field(default_factory=list)
    missing_artifact_ids: list[str] = Field(default_factory=list)
    added_artifact_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ReviewInputBaseline(ContractModel):
    contract: Literal["review_input_baseline.v1"] = "review_input_baseline.v1"
    run_id: str = Field(min_length=1)
    artifacts: list[ReviewInputFingerprint] = Field(default_factory=list)

    def compare(
        self, current: list[ReviewInputFingerprint]
    ) -> InputBaselineComparison:
        baseline_by_id = {artifact.artifact_id: artifact for artifact in self.artifacts}
        current_by_id = {artifact.artifact_id: artifact for artifact in current}
        changed = sorted(
            artifact_id
            for artifact_id, baseline in baseline_by_id.items()
            if artifact_id in current_by_id
            and baseline.sha256 != current_by_id[artifact_id].sha256
        )
        missing = sorted(set(baseline_by_id) - set(current_by_id))
        added = sorted(set(current_by_id) - set(baseline_by_id))
        reasons = [
            *(f"input changed: {artifact_id}" for artifact_id in changed),
            *(f"input missing: {artifact_id}" for artifact_id in missing),
            *(f"input added: {artifact_id}" for artifact_id in added),
        ]
        return InputBaselineComparison(
            full_review_required=bool(changed or missing or added),
            changed_artifact_ids=changed,
            missing_artifact_ids=missing,
            added_artifact_ids=added,
            reasons=reasons,
        )


class ChangedHunk(ContractModel):
    start_line: int = Field(ge=1)
    line_count: int = Field(ge=0)

    @property
    def end_line(self) -> int:
        return self.start_line + max(0, self.line_count - 1)


class DiffFile(ContractModel):
    repo_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    change_type: ChangeType
    surface: Optional[Surface] = None
    previous_path: Optional[str] = None
    old_hunks: list[ChangedHunk] = Field(default_factory=list)
    new_hunks: list[ChangedHunk] = Field(default_factory=list)


class RepositoryDiff(ContractModel):
    repo_id: str = Field(min_length=1)
    base_revision: Optional[str] = None
    head_revision: Optional[str] = None
    comparable: bool
    files: list[DiffFile] = Field(default_factory=list)
    error_code: Optional[str] = None
    working_tree_included: bool = False
    code_state_id: Optional[str] = None


class DiffResult(ContractModel):
    contract: Literal["diff_result.v1"] = "diff_result.v1"
    baseline_run_id: Optional[str] = None
    repositories: list[RepositoryDiff] = Field(default_factory=list)
    files: list[DiffFile] = Field(default_factory=list)
    comparable: bool
    errors: list[str] = Field(default_factory=list)
    unmapped_repo_ids: list[str] = Field(default_factory=list)
    input_preflight: Optional[InputBaselineComparison] = None


class CoverageImpact(ContractModel):
    coverage_unit_id: str = Field(min_length=1)
    affected: bool
    decision: Literal["affected", "unaffected"] = "affected"
    reasons: list[str] = Field(default_factory=list)
    repository_ids: list[str] = Field(default_factory=list)
    changed_file_refs: list[str] = Field(default_factory=list)
    changed_hunk_refs: list[str] = Field(default_factory=list)


class ImpactWorkItem(ContractModel):
    impact_work_item_id: str = Field(pattern=r"^iwi\.[A-Za-z0-9_.-]+$")
    coverage_unit_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    surface: Surface
    repository_ids: list[str] = Field(min_length=1)
    evidence_requirement_ids: list[str] = Field(default_factory=list)
    baseline_anchor_locations: list[str] = Field(default_factory=list)
    changed_files: list[DiffFile] = Field(default_factory=list)
    code_state_ids: dict[str, str] = Field(default_factory=dict)
    max_tool_rounds: int = Field(default=4, ge=1, le=12)


class ImpactDecision(ContractModel):
    coverage_unit_id: str = Field(min_length=1)
    status: Literal["affected", "unaffected"]
    reasons: list[str] = Field(min_length=1)
    changed_file_refs: list[str] = Field(default_factory=list)
    changed_hunk_refs: list[str] = Field(default_factory=list)
    graph_refs: list[str] = Field(default_factory=list)


class ImpactValidationResult(ContractModel):
    contract: Literal["impact_validation.v1"] = "impact_validation.v1"
    decisions: list[ImpactDecision] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReuseDecision(ContractModel):
    coverage_unit_id: str = Field(min_length=1)
    reusable: bool
    current_fingerprint: str = Field(min_length=1)
    previous_fingerprint: Optional[str] = None
    previous_run_id: Optional[str] = None
    result_origin_run_id: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)


class ReusePlan(ContractModel):
    contract: Literal["reuse_plan.v1"] = "reuse_plan.v1"
    baseline_run_id: Optional[str] = None
    review_unit_ids: list[str] = Field(default_factory=list)
    reused_unit_ids: list[str] = Field(default_factory=list)
    terminal_non_review_unit_ids: list[str] = Field(default_factory=list)
    decisions: list[ReuseDecision] = Field(default_factory=list)
    complete: bool


class RegressionChange(ContractModel):
    coverage_unit_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    previous_status: Optional[ControlStatus] = None
    current_status: ControlStatus
    classification: Literal["regression", "warning", "improvement", "unchanged"]
    reason: str = Field(min_length=1)


class RegressionComparison(ContractModel):
    contract: Literal["regression_comparison.v1"] = "regression_comparison.v1"
    baseline_run_id: Optional[str] = None
    current_run_id: str = Field(min_length=1)
    changes: list[RegressionChange] = Field(default_factory=list)
    ci_status: CiStatus
