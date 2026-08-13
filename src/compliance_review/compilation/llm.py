from __future__ import annotations

import json
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from compliance_review.compilation.models import ControlDraftSet, ObligationSet
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
) -> T:
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
        )
    )
    if response.tool_calls:
        raise ValueError(f"{request_kind} must not request tools")
    if not response.content:
        raise ValueError(f"{request_kind} returned empty structured content")
    return output_model.model_validate(_parse_json(response.content))


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) >= 3 else text.strip("`")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("structured compiler output must be a JSON object")
    return value


def obligation_extraction_call(provider: ModelProvider, payload: Any) -> ObligationSet:
    return structured_call(
        provider,
        work_item_id="phase2.obligation_extraction",
        request_kind="obligation_extraction",
        system_prompt=(
            "Extract regulatory obligations from the supplied source sections. "
            "Return JSON matching obligation_set.v1 with an obligations array. "
            "Do not invent requirements or combine unrelated sections. Every obligation "
            "must preserve source_id, source_section, statement, concepts, applicability "
            "expression, required_surfaces, and source_refs. Use only supplied source IDs "
            "and section IDs. This is one structured call, not repository exploration."
        ),
        user_payload=payload,
        output_model=ObligationSet,
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
    )


class ObligationExtractor:
    """Run the single structured call that extracts obligations from sources."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def extract(self, registry_payload: Any) -> ObligationSet:
        return obligation_extraction_call(self.provider, registry_payload)


class ControlCompiler:
    """Run the single structured call that compiles controls from obligations."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def compile(self, obligation_payload: Any) -> ControlDraftSet:
        return control_compilation_call(self.provider, obligation_payload)
