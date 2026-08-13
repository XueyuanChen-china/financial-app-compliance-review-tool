from __future__ import annotations

import json
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from compliance_review.compilation.models import (
    ControlDraftSet,
    ObligationExtractionBatchResult,
    SourceSectionBatch,
)
from compliance_review.domain.models import WorkItem
from compliance_review.review.models import ModelRequest
from compliance_review.review.provider import ModelProvider

T = TypeVar("T", bound=BaseModel)


def structured_call(
    provider: ModelProvider,
    *,
    work_item_id: str,
    request_kind: str,
    system_prompt: str,
    user_payload: Any,
    output_model: Type[T],
    response_schema: dict[str, Any] | None = None,
) -> T:
    return output_model.model_validate(
        _structured_response(
            provider,
            work_item_id=work_item_id,
            request_kind=request_kind,
            system_prompt=system_prompt,
            user_payload=user_payload,
            response_schema=response_schema,
        )
    )


def _structured_response(
    provider: ModelProvider,
    *,
    work_item_id: str,
    request_kind: str,
    system_prompt: str,
    user_payload: Any,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform exactly one structured model call; no tools and no agent loop."""
    work_item = WorkItem(
        work_item_id=work_item_id,
        module_id="phase2_compilation",
        surface="regulator_external",
        control_ids=["phase2"],
        max_tool_rounds=1,
        max_files_read=1,
        max_lines_per_read=1,
    )
    response = provider.complete(
        ModelRequest(
            work_item=work_item,
            attempt_id=work_item_id,
            agent_id="phase2-compiler",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            tools=[],
            token_budget=8000,
            request_kind=request_kind,  # type: ignore[arg-type]
            response_schema=response_schema,
        )
    )
    if response.tool_calls:
        raise ValueError(f"{request_kind} must not request tools")
    if not response.content:
        raise ValueError(f"{request_kind} returned empty structured content")
    return _parse_json(response.content)


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) >= 3 else text.strip("`")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("structured compiler output must be a JSON object")
    return value


def obligation_extraction_call(
    provider: ModelProvider, payload: dict[str, Any]
) -> ObligationExtractionBatchResult:
    return structured_call(
        provider,
        work_item_id=f"phase2.obligation_extraction.{payload['batch_id']}",
        request_kind="obligation_extraction",
        system_prompt=(
            "Extract regulatory obligations from only the supplied source sections. "
            "Return JSON matching obligation_extraction_batch.v1. For every supplied section, "
            "emit exactly one section_decisions terminal record: obligations_extracted with "
            "obligation_ids, or no_obligation with a short reason. "
            "Do not invent requirements or combine unrelated sections. Every obligation "
            "must preserve source_id, source_section, statement, concepts, applicability "
            "expression, required_surfaces, and source_refs. Use only supplied source IDs "
            "and section IDs. This is one structured call, not repository exploration."
        ),
        user_payload=payload,
        output_model=ObligationExtractionBatchResult,
        response_schema=ObligationExtractionBatchResult.model_json_schema(),
    )


def control_compilation_call(provider: ModelProvider, payload: Any) -> ControlDraftSet:
    return structured_call(
        provider,
        work_item_id="phase2.control_compilation",
        request_kind="control_compilation",
        system_prompt=(
            "Compile executable compliance controls only from the supplied obligations. "
            "Do not read or infer directly from source text. Return JSON matching "
            "control_draft_set.v1 with status draft. Preserve obligation_ids and source_refs. "
            "Use the finite applicability DSL: field == value, field includes value, "
            "value in field, joined only by and/&&. Every required surface must have an "
            "evidence requirement. Do not claim that a document proves source code or runtime."
        ),
        user_payload=payload,
        output_model=ControlDraftSet,
        response_schema=ControlDraftSet.model_json_schema(),
    )


class ObligationExtractor:
    """Extract obligations one bounded source batch at a time."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def extract(self, batch: SourceSectionBatch) -> ObligationExtractionBatchResult:
        payload = {
            "contract": "source_section_batch.v1",
            "batch_id": batch.batch_id,
            "source_id": batch.source_id,
            "sources": [
                {
                    "source_id": batch.source_id,
                    "sections": [section.model_dump(mode="json") for section in batch.sections],
                }
            ],
        }
        result = obligation_extraction_call(self.provider, payload)
        if result.source_id != batch.source_id or result.batch_id != batch.batch_id:
            raise ValueError("obligation extraction result does not match the source batch")
        return result


class ControlCompiler:
    """Run the single structured call that compiles controls from obligations."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def compile(self, obligation_payload: Any) -> ControlDraftSet:
        return control_compilation_call(self.provider, obligation_payload)
