from __future__ import annotations

import json
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from compliance_review.compilation.models import (
    ControlDraft,
    ControlDraftSet,
    ControlDraftSetTransport,
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
            "and section IDs. The applicability_expression must use only this finite DSL: "
            "business_type includes personal_loan; evidence_surfaces includes android_native; "
            "self_lending == true; jurisdiction == Pakistan; clauses may be joined by "
            "and or &&. If the source condition cannot be represented exactly in that DSL, "
            "return exactly unknown instead of natural-language prose or a new field. "
            "Use business_type includes personal_loan whenever the source says personal "
            "loan or personal loan app, and use jurisdiction == Pakistan for an explicit "
            "Pakistan condition; do not use unknown for those exact mappings. Use unknown "
            "for rate thresholds, licensing/document requirements, or multi-country logic "
            "that the DSL cannot represent exactly. "
            "This is one structured call, not repository exploration."
        ),
        user_payload=payload,
        output_model=ObligationExtractionBatchResult,
        response_schema=ObligationExtractionBatchResult.model_json_schema(),
    )


def control_compilation_call(provider: ModelProvider, payload: Any) -> ControlDraftSet:
    transport = structured_call(
        provider,
        work_item_id="phase2.control_compilation",
        request_kind="control_compilation",
        system_prompt=(
            "Compile executable compliance controls only from the supplied obligations. "
            "Do not read or infer directly from source text. Return JSON matching "
            "control_draft_set.v1 with status draft. Each control must include exactly "
            "control_id, module_id, title, severity, obligation_ids, "
            "evidence_requirements, missing_evidence_policy, and "
            "reuse_invalidation_keys. Preserve obligation_ids exactly. "
            "evidence_requirements must be an array of objects with surface, "
            "minimum_strength, and rationale; do not return it as a keyed object. "
            "The program derives source_refs, required_surfaces, and "
            "applicability_expression from the linked obligations, so do not output "
            "those fields. Every derived required surface must have exactly one "
            "evidence requirement. Do not claim that a document proves source code "
            "or runtime."
        ),
        user_payload=payload,
        output_model=ControlDraftSetTransport,
        response_schema=ControlDraftSetTransport.model_json_schema(),
    )
    obligations = {
        item["obligation_id"]: item for item in payload.get("obligations", [])
    }
    controls: list[ControlDraft] = []
    for draft in transport.controls:
        linked_obligations = [obligations.get(item) for item in draft.obligation_ids]
        if any(item is None for item in linked_obligations):
            missing = [
                item
                for item, obligation in zip(draft.obligation_ids, linked_obligations)
                if obligation is None
            ]
            raise ValueError(
                f"control {draft.control_id} references unknown obligations: {', '.join(missing)}"
            )
        source_refs: list[dict[str, Any]] = []
        source_ref_keys: set[str] = set()
        required_surfaces: list[str] = []
        for obligation in linked_obligations:
            assert obligation is not None
            for source_ref in obligation["source_refs"]:
                key = json.dumps(source_ref, sort_keys=True)
                if key not in source_ref_keys:
                    source_ref_keys.add(key)
                    source_refs.append(source_ref)
            for surface in obligation["required_surfaces"]:
                if surface not in required_surfaces:
                    required_surfaces.append(surface)
        expressions = {
            obligation["applicability_expression"]
            for obligation in linked_obligations
            if obligation is not None
        }
        applicability_expression = expressions.pop() if len(expressions) == 1 else "unknown"
        evidence_requirements: dict[str, Any] = {}
        for item in draft.evidence_requirements:
            if item.surface in evidence_requirements:
                raise ValueError(
                    f"control {draft.control_id} has duplicate evidence surface: "
                    f"{item.surface}"
                )
            evidence_requirements[item.surface] = {
                "minimum_strength": item.minimum_strength,
                "rationale": item.rationale,
            }
        controls.append(
            ControlDraft.model_validate(
                {
                    **draft.model_dump(exclude={"evidence_requirements"}),
                    "source_refs": source_refs,
                    "applicability_expression": applicability_expression,
                    "required_surfaces": required_surfaces,
                    "evidence_requirements": evidence_requirements,
                }
            )
        )
    return ControlDraftSet(
        contract=transport.contract,
        version=transport.version,
        status=transport.status,
        controls=controls,
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
