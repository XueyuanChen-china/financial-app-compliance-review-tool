from __future__ import annotations

from collections import defaultdict
from math import ceil

from compliance_review.compilation.models import (
    Obligation,
    ObligationExtractionBatchResult,
    SectionCoverageDecision,
    SourceRegistry,
    SourceSection,
    SourceSectionBatch,
)

_DEFAULT_INPUT_OVERHEAD_TOKENS = 128
_DEFAULT_SECTION_OVERHEAD_TOKENS = 32


def estimate_tokens(text: str) -> int:
    """Estimate tokens conservatively without requiring a model tokenizer.

    ASCII text is approximated at four characters per token. Non-ASCII text is
    counted at one character per token because this is safer for CJK and other
    multilingual policy materials than applying the ASCII ratio universally.
    """
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_count = len(text) - non_ascii
    return max(1, ceil(ascii_count / 4 + non_ascii))


class BatchPlanner:
    """Pack complete sections greedily without mixing source files."""

    def __init__(
        self,
        max_input_tokens: int = 8000,
        input_overhead_tokens: int = _DEFAULT_INPUT_OVERHEAD_TOKENS,
        section_overhead_tokens: int = _DEFAULT_SECTION_OVERHEAD_TOKENS,
    ) -> None:
        if max_input_tokens < 256:
            raise ValueError("max_input_tokens must be at least 256")
        if input_overhead_tokens < 0 or section_overhead_tokens < 0:
            raise ValueError("batch overhead tokens cannot be negative")
        if input_overhead_tokens >= max_input_tokens:
            raise ValueError("input_overhead_tokens must be smaller than max_input_tokens")
        self.max_input_tokens = max_input_tokens
        self.input_overhead_tokens = input_overhead_tokens
        self.section_overhead_tokens = section_overhead_tokens

    def plan(self, registry: SourceRegistry) -> list[SourceSectionBatch]:
        batches: list[SourceSectionBatch] = []
        batch_number = 1
        for source in registry.sources:
            sections = sorted(source.sections, key=lambda item: item.ordinal)
            current: list[SourceSection] = []
            current_tokens = self.input_overhead_tokens
            for section in sections:
                section_tokens = (
                    estimate_tokens(section.text)
                    + estimate_tokens(section.title)
                    + self.section_overhead_tokens
                )
                if self.input_overhead_tokens + section_tokens > self.max_input_tokens:
                    raise ValueError(
                        f"source section exceeds batch input budget: "
                        f"{source.source_id}/{section.section_id} requires "
                        f"{self.input_overhead_tokens + section_tokens} tokens, "
                        f"budget is {self.max_input_tokens}"
                    )
                if current and current_tokens + section_tokens > self.max_input_tokens:
                    batches.append(
                        self._batch(source.source_id, batch_number, current, current_tokens)
                    )
                    batch_number += 1
                    current = []
                    current_tokens = self.input_overhead_tokens
                current.append(section)
                current_tokens += section_tokens
            if current:
                batches.append(self._batch(source.source_id, batch_number, current, current_tokens))
                batch_number += 1
        return batches

    @staticmethod
    def _batch(
        source_id: str, number: int, sections: list[SourceSection], tokens: int
    ) -> SourceSectionBatch:
        return SourceSectionBatch(
            batch_id=f"batch-{number:04d}",
            source_id=source_id,
            sections=list(sections),
            estimated_input_tokens=tokens,
        )


def validate_batch_coverage(
    batch: SourceSectionBatch, result: ObligationExtractionBatchResult
) -> None:
    """Require exactly one terminal decision for every planned section in a batch."""
    planned = {section.section_id for section in batch.sections}
    if result.source_id != batch.source_id or result.batch_id != batch.batch_id:
        raise ValueError("obligation batch result does not match the planned batch")
    counts: defaultdict[str, int] = defaultdict(int)
    for decision in result.section_decisions:
        counts[decision.section_id] += 1
        if decision.section_id not in planned:
            raise ValueError(f"decision references unknown source section: {decision.section_id}")
        _validate_decision(decision)
    missing = sorted(section_id for section_id in planned if counts[section_id] == 0)
    duplicates = sorted(section_id for section_id, count in counts.items() if count > 1)
    if missing:
        raise ValueError(f"missing section terminal decision: {', '.join(missing)}")
    if duplicates:
        raise ValueError(f"duplicate section terminal decision: {', '.join(duplicates)}")

    obligation_map = {item.obligation_id: item for item in result.obligations}
    decided_ids = {
        obligation_id
        for decision in result.section_decisions
        for obligation_id in decision.obligation_ids
    }
    unknown_ids = sorted(decided_ids - set(obligation_map))
    if unknown_ids:
        raise ValueError(
            "section decisions reference unknown obligations: " + ", ".join(unknown_ids)
        )
    undeclared = sorted(set(obligation_map) - decided_ids)
    if undeclared:
        raise ValueError(f"obligations missing from section decisions: {', '.join(undeclared)}")
    for obligation in result.obligations:
        if obligation.source_id != batch.source_id or obligation.source_section not in planned:
            raise ValueError(
                f"obligation {obligation.obligation_id} references a section outside its batch"
            )


def _validate_decision(decision: SectionCoverageDecision) -> None:
    if decision.decision == "obligations_extracted":
        if not decision.obligation_ids:
            raise ValueError(
                f"section {decision.section_id} must list obligation_ids when obligations exist"
            )
        if decision.reason is not None:
            raise ValueError(f"section {decision.section_id} has an invalid extraction reason")
    elif decision.obligation_ids:
        raise ValueError(f"section {decision.section_id} cannot list obligations for no_obligation")
    elif not decision.reason:
        raise ValueError(f"section {decision.section_id} needs a no_obligation reason")


def merge_obligation_batches(
    results: list[ObligationExtractionBatchResult],
) -> list[Obligation]:
    """Merge results deterministically and reject conflicting duplicate IDs."""
    by_id: dict[str, Obligation] = {}
    for result in sorted(results, key=lambda item: item.batch_id):
        for obligation in sorted(result.obligations, key=lambda item: item.obligation_id):
            previous = by_id.get(obligation.obligation_id)
            if previous is not None and previous.model_dump(mode="json") != obligation.model_dump(
                mode="json"
            ):
                raise ValueError(f"conflicting duplicate obligation_id: {obligation.obligation_id}")
            by_id[obligation.obligation_id] = obligation
    return [by_id[key] for key in sorted(by_id)]


def validate_registry_coverage(
    registry: SourceRegistry, results: list[ObligationExtractionBatchResult]
) -> None:
    planned = {
        (source.source_id, section.section_id)
        for source in registry.sources
        for section in source.sections
    }
    completed: list[tuple[str, str]] = []
    for result in results:
        completed.extend(
            (result.source_id, decision.section_id) for decision in result.section_decisions
        )
    counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    for key in completed:
        counts[key] += 1
    missing = sorted(planned - set(counts))
    unknown = sorted(set(counts) - planned)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    errors: list[str] = []
    if missing:
        errors.append(
            "missing registry section decisions: "
            + ", ".join(_format_key(key) for key in missing)
        )
    if unknown:
        errors.append(
            "unknown registry section decisions: "
            + ", ".join(_format_key(key) for key in unknown)
        )
    if duplicates:
        errors.append(
            "duplicate registry section decisions: "
            + ", ".join(_format_key(key) for key in duplicates)
        )
    if errors:
        raise ValueError("; ".join(errors))


def _format_key(key: tuple[str, str]) -> str:
    return f"{key[0]}/{key[1]}"
