from __future__ import annotations

import json
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compliance_review.domain.models import Surface, WorkItem


class ReviewerContextError(RuntimeError):
    """Base error for a Work Item context that cannot continue safely."""


class ContextBudgetExceeded(ReviewerContextError):
    """The current Work Item cannot fit within its configured context budget."""


class ReviewerRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=3, ge=1, le=32)
    active_window_size: int = Field(default=3, ge=1, le=20)
    compression_trigger: float = Field(default=0.78, gt=0, le=1)
    compression_target: float = Field(default=0.60, gt=0, le=1)
    hard_limit: float = Field(default=0.90, gt=0, le=1)
    max_compression_attempts: int = Field(default=2, ge=1, le=5)
    context_window_tokens: int = Field(default=12000, ge=100)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "ReviewerRuntimeConfig":
        if not self.compression_target < self.compression_trigger < self.hard_limit:
            raise ValueError("compression_target < compression_trigger < hard_limit is required")
        return self


class ReviewerImmutableContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item: WorkItem
    required_surface: Surface
    reviewer_instructions: str = Field(min_length=1)


class EvidenceAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_ids: list[str] = Field(min_length=1)
    source_tool: str = Field(min_length=1)
    path: Optional[str] = None
    symbol: Optional[str] = None
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    summary: str = Field(min_length=1)


class AgentRound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_number: int = Field(ge=1)
    model_response: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)


class CompressedReviewMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: int = Field(ge=1)
    inspected_paths: list[str] = Field(default_factory=list)
    inspected_symbols: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    dead_ends: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    next_search_hints: list[str] = Field(default_factory=list)


class ReviewerContextState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    immutable_context: ReviewerImmutableContext
    evidence_ledger: list[EvidenceAnchor] = Field(default_factory=list)
    compressed_memory: Optional[CompressedReviewMemory] = None
    retired_rounds: list[AgentRound] = Field(default_factory=list)
    active_rounds: list[AgentRound] = Field(default_factory=list)
    compression_attempts: int = Field(default=0, ge=0)
    last_context_usage_ratio: float = Field(default=0, ge=0)


CompressionFunction = Callable[
    [Optional[CompressedReviewMemory], list[AgentRound]], CompressedReviewMemory
]


class ReviewerContextManager:
    """Maintain one Work Item's durable state and bounded working memory."""

    def __init__(self, config: ReviewerRuntimeConfig) -> None:
        self.config = config

    def create(self, work_item: WorkItem, instructions: str) -> ReviewerContextState:
        return ReviewerContextState(
            immutable_context=ReviewerImmutableContext(
                work_item=work_item,
                required_surface=work_item.surface,
                reviewer_instructions=instructions,
            )
        )

    def record_round(
        self, state: ReviewerContextState, round_item: AgentRound
    ) -> ReviewerContextState:
        next_state = state.model_copy(deep=True)
        next_state.active_rounds.append(round_item)
        while len(next_state.active_rounds) > self.config.active_window_size:
            next_state.retired_rounds.append(next_state.active_rounds.pop(0))
        return next_state

    def add_evidence_anchors(
        self, state: ReviewerContextState, anchors: list[EvidenceAnchor]
    ) -> ReviewerContextState:
        next_state = state.model_copy(deep=True)
        existing = {self._anchor_key(anchor) for anchor in next_state.evidence_ledger}
        for anchor in anchors:
            if self._anchor_key(anchor) not in existing:
                next_state.evidence_ledger.append(anchor)
                existing.add(self._anchor_key(anchor))
        return next_state

    def render_messages(
        self, state: ReviewerContextState, include_retired: bool = False
    ) -> list[dict[str, Any]]:
        immutable = state.immutable_context
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": immutable.reviewer_instructions},
            {
                "role": "user",
                "content": json.dumps(
                    immutable.work_item.model_dump(), sort_keys=True
                ),
            },
        ]
        if state.evidence_ledger:
            messages.append(
                {
                    "role": "user",
                    "content": "Durable evidence ledger:\n"
                    + json.dumps(
                        [anchor.model_dump() for anchor in state.evidence_ledger],
                        sort_keys=True,
                    ),
                }
            )
        if state.compressed_memory is not None:
            messages.append(
                {
                    "role": "assistant",
                    "content": "Compressed exploration memory:\n"
                    + state.compressed_memory.model_dump_json(),
                }
            )
        rounds = state.active_rounds
        if include_retired:
            rounds = [*state.retired_rounds, *state.active_rounds]
        for round_item in rounds:
            response = round_item.model_response
            if response.get("tool_calls"):
                messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": response["tool_calls"],
                    }
                )
            elif response.get("content"):
                messages.append({"role": "assistant", "content": response["content"]})
            for tool_result in round_item.tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_result.get("call_id"),
                        "content": json.dumps(tool_result, sort_keys=True),
                    }
                )
        return messages

    def estimate_usage(self, messages: list[dict[str, Any]]) -> int:
        return max(1, (len(json.dumps(messages, ensure_ascii=False)) + 3) // 4)

    def usage_ratio(self, messages: list[dict[str, Any]]) -> float:
        return self.estimate_usage(messages) / self.config.context_window_tokens

    def prepare_for_model(
        self,
        state: ReviewerContextState,
        messages: list[dict[str, Any]],
        compressor: CompressionFunction,
    ) -> tuple[ReviewerContextState, list[dict[str, Any]]]:
        usage_ratio = self.usage_ratio(messages)
        current = state.model_copy(deep=True)
        current.last_context_usage_ratio = usage_ratio
        if usage_ratio < self.config.compression_trigger:
            return current, messages
        if not current.retired_rounds and current.compressed_memory is None:
            raise ContextBudgetExceeded("context_budget_exhausted")

        original_memory = current.compressed_memory
        original_retired = list(current.retired_rounds)
        for attempt in range(1, self.config.max_compression_attempts + 1):
            try:
                compressed = compressor(original_memory, list(original_retired))
            except Exception as exc:
                if attempt == self.config.max_compression_attempts:
                    raise ContextBudgetExceeded("context_budget_exhausted") from exc
                continue
            candidate = current.model_copy(deep=True)
            candidate.compressed_memory = compressed
            candidate.retired_rounds = []
            candidate.compression_attempts = attempt
            candidate_messages = self.render_messages(candidate)
            candidate.last_context_usage_ratio = self.usage_ratio(candidate_messages)
            if candidate.last_context_usage_ratio <= self.config.compression_target:
                candidate.compression_attempts = 0
                return candidate, candidate_messages
        raise ContextBudgetExceeded("context_budget_exhausted")

    @staticmethod
    def _anchor_key(anchor: EvidenceAnchor) -> tuple[Any, ...]:
        return (
            tuple(anchor.control_ids),
            anchor.source_tool,
            anchor.path,
            anchor.symbol,
            anchor.start_line,
            anchor.end_line,
        )
