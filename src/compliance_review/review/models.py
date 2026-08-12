from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from compliance_review.domain.models import ReviewMode, ReviewResult, Surface, WorkItem


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
    surface_roots: dict[Surface, str] = Field(default_factory=dict)
    work_items: list[WorkItem] = Field(default_factory=list)
    excluded_controls: list[ExcludedControl] = Field(default_factory=list)
    source_profile_version: str = Field(min_length=1)
    source_control_version: str = Field(min_length=1)


class ToolCall(ReviewContractModel):
    call_id: str = Field(min_length=1)
    name: Literal["list_files", "search_code", "read_file"]
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(ReviewContractModel):
    work_item: WorkItem
    attempt_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    token_budget: int = Field(default=4000, ge=100)


class ModelResponse(ReviewContractModel):
    content: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


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
    context_fingerprint: str = Field(min_length=1)


class ReviewRunSummary(ReviewContractModel):
    run_id: str = Field(min_length=1)
    executions: list[WorkerExecution] = Field(default_factory=list)
    max_concurrency: int = Field(ge=1)
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    event_log_path: str = Field(min_length=1)


class ScopedToolResult(ReviewContractModel):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ok: bool
    output: Any = None
    error: Optional[str] = None


def work_item_surface(work_item: WorkItem) -> Surface:
    return work_item.surface
