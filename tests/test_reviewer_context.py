from __future__ import annotations

from typing import Any

import pytest

from compliance_review.domain.models import WorkItem
from compliance_review.review.context import (
    AgentRound,
    CompressedReviewMemory,
    ContextBudgetExceeded,
    EvidenceAnchor,
    ReviewerContextManager,
    ReviewerRuntimeConfig,
)


def _work_item() -> WorkItem:
    return WorkItem(
        work_item_id="wi.context",
        module_id="data_privacy_and_permissions",
        surface="backend_code",
        control_ids=["C001"],
        allowed_roots=["backend"],
        max_tool_rounds=4,
        max_files_read=2,
        max_lines_per_read=20,
    )


def _round(number: int, content: str = "small") -> AgentRound:
    return AgentRound(
        round_number=number,
        model_response={"content": content},
        estimated_tokens=max(1, len(content) // 4),
    )


def _manager(**overrides: Any) -> ReviewerContextManager:
    options = {"context_window_tokens": 1000, **overrides}
    config = ReviewerRuntimeConfig(**options)
    return ReviewerContextManager(config)


def test_active_window_slides_without_immediate_compression() -> None:
    manager = _manager()
    state = manager.create(_work_item(), "review instructions")

    for number in range(1, 5):
        state = manager.record_round(state, _round(number))

    assert [item.round_number for item in state.active_rounds] == [2, 3, 4]
    assert [item.round_number for item in state.retired_rounds] == [1]
    assert state.compressed_memory is None


def test_below_trigger_does_not_compress() -> None:
    manager = _manager()
    state = manager.create(_work_item(), "review instructions")
    calls = 0

    def compressor(
        memory: CompressedReviewMemory | None, retired: list[AgentRound]
    ) -> CompressedReviewMemory:
        nonlocal calls
        calls += 1
        return CompressedReviewMemory(generation=1)

    prepared, _ = manager.prepare_for_model(
        state, manager.render_messages(state), compressor
    )

    assert calls == 0
    assert prepared.compressed_memory is None


def test_compression_is_structured_and_preserves_immutable_and_evidence() -> None:
    manager = _manager()
    state = manager.create(_work_item(), "immutable instructions")
    anchor = EvidenceAnchor(
        control_ids=["C001"],
        source_tool="read_file",
        path="backend/service.py",
        start_line=10,
        end_line=12,
        summary="service evidence",
    )
    state = manager.add_evidence_anchors(state, [anchor])
    state = manager.record_round(state, _round(1, "x" * 4000))
    for number in range(2, 5):
        state = manager.record_round(state, _round(number))

    seen: list[tuple[Any, list[AgentRound]]] = []

    def compressor(
        memory: CompressedReviewMemory | None, retired: list[AgentRound]
    ) -> CompressedReviewMemory:
        seen.append((memory, retired))
        return CompressedReviewMemory(
            generation=1,
            inspected_paths=["backend/service.py"],
            next_search_hints=["check repository delete path"],
        )

    prepared, _ = manager.prepare_for_model(
        state, manager.render_messages(state, include_retired=True), compressor
    )

    assert len(seen) == 1
    assert [item.round_number for item in seen[0][1]] == [1]
    assert prepared.compressed_memory is not None
    assert prepared.compressed_memory.inspected_paths == ["backend/service.py"]
    assert prepared.retired_rounds == []
    assert [item.round_number for item in prepared.active_rounds] == [2, 3, 4]
    assert prepared.immutable_context.reviewer_instructions == "immutable instructions"
    assert prepared.evidence_ledger == [anchor]
    assert prepared.last_context_usage_ratio <= 0.60


def test_compression_retries_against_original_retired_rounds() -> None:
    manager = _manager()
    state = manager.create(_work_item(), "instructions")
    state = manager.record_round(state, _round(1, "x" * 3500))
    for number in range(2, 5):
        state = manager.record_round(state, _round(number))
    attempts: list[list[int]] = []

    def compressor(
        memory: CompressedReviewMemory | None, retired: list[AgentRound]
    ) -> CompressedReviewMemory:
        attempts.append([item.round_number for item in retired])
        if len(attempts) == 1:
            return CompressedReviewMemory(generation=1, findings=["x" * 3500])
        return CompressedReviewMemory(generation=1, findings=["compact finding"])

    prepared, _ = manager.prepare_for_model(
        state, manager.render_messages(state, include_retired=True), compressor
    )

    assert attempts == [[1], [1]]
    assert prepared.compression_attempts == 0
    assert prepared.compressed_memory is not None
    assert prepared.compressed_memory.findings == ["compact finding"]


def test_compression_failure_preserves_context_and_hard_limit_is_indeterminate() -> None:
    manager = _manager()
    state = manager.create(_work_item(), "instructions")
    state = manager.add_evidence_anchors(
        state,
        [
            EvidenceAnchor(
                control_ids=["C001"],
                source_tool="search_code",
                path="backend/service.py",
                summary="durable anchor",
            )
        ],
    )
    state = manager.record_round(state, _round(1, "x" * 3500))
    for number in range(2, 5):
        state = manager.record_round(state, _round(number))
    before = state.model_dump()

    def failing_compressor(
        memory: CompressedReviewMemory | None, retired: list[AgentRound]
    ) -> CompressedReviewMemory:
        raise RuntimeError("compressor unavailable")

    with pytest.raises(ContextBudgetExceeded, match="context_budget_exhausted"):
        manager.prepare_for_model(
            state, manager.render_messages(state, include_retired=True), failing_compressor
        )
    assert state.model_dump() == before

    tiny_manager = _manager(context_window_tokens=100)
    tiny_state = tiny_manager.create(_work_item(), "instructions")
    oversized = tiny_manager.render_messages(tiny_state) + [
        {"role": "user", "content": "x" * 1000}
    ]
    with pytest.raises(ContextBudgetExceeded, match="context_budget_exhausted"):
        tiny_manager.prepare_for_model(tiny_state, oversized, failing_compressor)
