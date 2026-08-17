from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from compliance_review.domain.models import (
    ControlStatus,
    ControlSurfaceResult,
    CoverageGateResult,
    ResolvedControlResult,
    ReviewMode,
    ReviewResult,
    Snapshot,
    Surface,
    WorkItem,
)


class ReviewContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExcludedControl(ReviewContractModel):
    control_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReviewManifest(ReviewContractModel):
    contract: Literal["review_manifest.v1"]
    run_id: str = Field(min_length=1)
    mode: ReviewMode
    default_max_concurrency: int = Field(default=3, ge=1, le=32)
    surface_roots: dict[str, str] = Field(default_factory=dict)
    work_items: list[WorkItem] = Field(default_factory=list)
    excluded_controls: list[ExcludedControl] = Field(default_factory=list)
    coverage_unit_ids: list[str] = Field(default_factory=list)
    unknown_control_ids: list[str] = Field(default_factory=list)
    missing_surfaces: list[Surface] = Field(default_factory=list)
    source_profile_version: str = Field(min_length=1)
    source_control_version: str = Field(min_length=1)


class ToolCall(ReviewContractModel):
    call_id: str = Field(min_length=1)
    name: Literal[
        "code_map_query",
        "code_map_path",
        "get_collector_facts",
        "get_repository_inventory",
        "get_app_facts",
        "list_files",
        "search_code",
        "read_file",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(ReviewContractModel):
    work_item: WorkItem
    attempt_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    token_budget: int = Field(default=4000, ge=100)
    response_schema: Optional[dict[str, Any]] = None
    request_kind: Literal[
        "review",
        "compression",
        "obligation_extraction",
        "control_compilation",
        "applicability",
        "review_finalization",
        "verification",
    ] = "review"


class ModelResponse(ReviewContractModel):
    content: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerAttempt(ReviewContractModel):
    work_item_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    status: Literal["pending", "running", "completed", "failed", "interrupted"]
    started_at: str = Field(min_length=1)
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False
    context_fingerprint: str = Field(min_length=1)
    result_ref: Optional[str] = None
    predecessor_attempt_id: Optional[str] = None


def validate_review_result_assignment(
    result: ReviewResult,
    work_item: WorkItem,
    attempt_id: str,
    agent_id: str,
) -> None:
    """Require an exact, duplicate-free result for the assigned Work Item."""
    if result.work_item_id != work_item.work_item_id:
        raise ValueError("review result work_item_id does not match assigned work item")
    if result.attempt_id != attempt_id:
        raise ValueError("review result attempt_id does not match current attempt")
    if result.agent_id != agent_id:
        raise ValueError("review result agent_id does not match current worker")
    if result.execution_status != "completed":
        raise ValueError("successful reviewer response must have completed execution status")
    expected = {(control_id, work_item.surface) for control_id in work_item.control_ids}
    actual = [(row.control_id, row.surface) for row in result.rows]
    actual_set = set(actual)
    if len(actual) != len(actual_set):
        raise ValueError("review result contains duplicate control-surface rows")
    if actual_set != expected:
        raise ValueError(
            "review result rows must exactly match assigned controls and surface: "
            f"expected={sorted(expected)} actual={sorted(actual_set)}"
        )


class WorkerExecution(ReviewContractModel):
    work_item_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    execution_status: Literal["completed", "failed"]
    result_path: str = Field(min_length=1)
    result: Optional[ReviewResult] = None
    tool_rounds: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    error: Optional[str] = None
    error_code: Optional[str] = None
    retryable: bool = False
    attempt_number: int = Field(default=1, ge=1)
    predecessor_attempt_id: Optional[str] = None
    context_fingerprint: str = Field(min_length=1)
    attempt: Optional[WorkerAttempt] = None


class ReviewRunSummary(ReviewContractModel):
    run_id: str = Field(min_length=1)
    executions: list[WorkerExecution] = Field(default_factory=list)
    max_concurrency: int = Field(ge=1)
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    event_log_path: str = Field(min_length=1)
    attempts: list[WorkerAttempt] = Field(default_factory=list)


class ValidationIssue(ReviewContractModel):
    code: str = Field(min_length=1)
    # "suspicious" is accepted only for reading older artifacts; new output uses "flag".
    severity: Literal["error", "flag", "suspicious"]
    message: str = Field(min_length=1)


class ValidatedReviewRow(ReviewContractModel):
    row_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    surface: Surface
    work_item_id: Optional[str] = None
    attempt_id: Optional[str] = None
    execution_status: Optional[Literal["completed", "failed"]] = None
    row: Optional[ControlSurfaceResult] = None
    valid: bool
    flags: list[str] = Field(default_factory=list)
    suspicious: bool = False
    result_origin: Literal["reviewed", "reused"] = "reviewed"
    previous_run_id: Optional[str] = None
    issues: list[ValidationIssue] = Field(default_factory=list)


class ResultValidationResult(ReviewContractModel):
    contract: Literal["result_validation.v1"] = "result_validation.v1"
    valid: bool
    rows: list[ValidatedReviewRow] = Field(default_factory=list)
    flags: dict[str, list[str]] = Field(default_factory=dict)
    suspicious_row_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SuspiciousReviewSet(ReviewContractModel):
    contract: Literal["suspicious_review_set.v1"] = "suspicious_review_set.v1"
    row_ids: list[str] = Field(default_factory=list)
    reasons: dict[str, list[str]] = Field(default_factory=dict)


class VerifierDecision(ReviewContractModel):
    row_id: str = Field(min_length=1)
    decision: Literal["confirm", "object", "correction"]
    reason: str = Field(min_length=1)
    recommended_status: Optional[ControlStatus] = None
    anchor_ids: list[str] = Field(default_factory=list)


class VerifierResult(ReviewContractModel):
    contract: Literal["verifier_result.v1"] = "verifier_result.v1"
    status: Literal["not_required", "completed", "partial", "failed"]
    decisions: list[VerifierDecision] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class FullReviewRunResult(ReviewContractModel):
    contract: Literal["full_review_run_result.v1"] = "full_review_run_result.v1"
    summary: ReviewRunSummary
    validation: ResultValidationResult
    suspicious: SuspiciousReviewSet
    verifier: VerifierResult
    resolved_controls: list[ResolvedControlResult]
    coverage_gate: CoverageGateResult
    snapshot: Snapshot
    report_path: str = Field(min_length=1)


class DiffReviewRunResult(FullReviewRunResult):
    diff_path: str = Field(min_length=1)
    impact_path: str = Field(min_length=1)
    reuse_plan_path: str = Field(min_length=1)
    regression_path: str = Field(min_length=1)


class ScopedToolResult(ReviewContractModel):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ok: bool
    output: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    retryable: bool = False


def work_item_surface(work_item: WorkItem) -> Surface:
    return work_item.surface
