from __future__ import annotations

import json
from copy import deepcopy
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


def _patch_strict_condition_value_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Give structured-output providers a finite condition transport schema.

    The domain model intentionally accepts JSON-like values here because an
    applicability atom may compare a string, boolean, number, or list.  Some
    strict OpenAI-compatible endpoints reject the Pydantic schema generated
    for ``Any`` (an empty schema with no ``type``).  The generated model schema
    also cannot express that condition ``kind`` selects mutually exclusive
    fields, so the transport schema makes the four legal shapes explicit.
    """
    normalized = deepcopy(schema)
    definitions = normalized.get("$defs", {})
    if not isinstance(definitions, dict) or "ApplicabilityCondition" not in definitions:
        return normalized

    value_schema = {
        "anyOf": [
            {"type": "string"},
            {"type": "boolean"},
            {"type": "number"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }
    condition_ref = {"$ref": "#/$defs/ApplicabilityCondition"}
    definitions["ApplicabilityCondition"] = {
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["atom"]},
                    "fact": {"type": "string"},
                    "operator": {"type": "string", "enum": ["equals", "includes"]},
                    "value": value_schema,
                },
                "required": ["kind", "fact", "operator", "value"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["all_of", "any_of"]},
                    "conditions": {"type": "array", "items": condition_ref},
                },
                "required": ["kind", "conditions"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["unknown"]},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "reason"],
                "additionalProperties": False,
            },
        ]
    }
    return normalized


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
        work_item_type="compilation",
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
    source_id = str(payload["source_id"])
    batch_id = str(payload["batch_id"])
    section_ids = [
        str(section["section_id"])
        for source in payload.get("sources", [])
        for section in source.get("sections", [])
    ]
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
            "condition, required_surfaces, and source_refs. Use only supplied source IDs "
            "and section IDs. The condition must be structured JSON: an atom has fact, "
            "operator (equals or includes), and value; combine atoms with all_of or any_of. "
            "Allowed fact names are only business_type, evidence_surfaces, self_lending, "
            "jurisdiction, offers_or_facilitates_loans, loan_application_flow_present, "
            "earned_wage_access, account_deletion_flow_present, and "
            "sensitive_permission_use_present. "
            "Use kind=unknown when the source semantics cannot be represented safely. Do not "
            "invent a string expression or a new operator. "
            "This is one structured call, not repository exploration."
        ),
        user_payload=payload,
        output_model=ObligationExtractionBatchResult,
        response_schema=_bounded_obligation_response_schema(
            source_id=source_id,
            batch_id=batch_id,
            section_ids=section_ids,
        ),
    )


def _bounded_obligation_response_schema(
    *, source_id: str, batch_id: str, section_ids: list[str]
) -> dict[str, Any]:
    """Restrict provenance fields to the exact current extraction batch.

    Pydantic validates the returned values after transport, but a provider can
    still emit a syntactically valid string that is a truncated or foreign ID.
    Dynamic enums make the same provenance boundary visible to strict structured
    output providers before the model produces the response.
    """
    schema = _patch_strict_condition_value_schema(
        ObligationExtractionBatchResult.model_json_schema()
    )
    allowed_facts = {
        "business_type",
        "evidence_surfaces",
        "self_lending",
        "jurisdiction",
        "offers_or_facilitates_loans",
        "loan_application_flow_present",
        "earned_wage_access",
        "account_deletion_flow_present",
        "sensitive_permission_use_present",
    }
    condition_schema = schema["$defs"]["ApplicabilityCondition"]
    condition_variants = condition_schema.get("anyOf", [condition_schema])
    for variant in condition_variants:
        fact_schema = variant.get("properties", {}).get("fact")
        if fact_schema is not None and fact_schema.get("type") == "string":
            fact_schema["enum"] = sorted(allowed_facts)
    schema["properties"]["source_id"]["enum"] = [source_id]
    schema["properties"]["batch_id"]["enum"] = [batch_id]
    schema["$defs"]["SectionCoverageDecision"]["properties"]["section_id"]["enum"] = section_ids

    obligation = schema["$defs"]["Obligation"]["properties"]
    obligation["source_id"]["enum"] = [source_id]
    obligation["source_section"]["enum"] = section_ids

    source_ref = schema["$defs"]["SourceRef"]["properties"]
    for field_name, allowed in {
        "source_id": [source_id],
        "source_section": section_ids,
    }.items():
        for option in source_ref[field_name]["anyOf"]:
            if option.get("type") == "string":
                option["enum"] = allowed
    return schema


def control_compilation_call(provider: ModelProvider, payload: Any) -> ControlDraftSet:
    obligation_ids = [
        str(item["obligation_id"])
        for item in payload.get("obligations", [])
        if isinstance(item, dict) and item.get("obligation_id")
    ]
    response_schema = _patch_strict_condition_value_schema(
        ControlDraftSetTransport.model_json_schema()
    )
    # Keep references inside the structured-output boundary. The deterministic
    # validator remains the final authority, but the model cannot invent IDs
    # that are absent from this compilation batch.
    for definition_name in ("ControlDraftTransport", "ControlEvidenceClaimItem"):
        response_schema["$defs"][definition_name]["properties"]["obligation_ids"][
            "items"
        ]["enum"] = obligation_ids
    transport = structured_call(
        provider,
        work_item_id="phase2.control_compilation",
        request_kind="control_compilation",
        system_prompt=(
            "Compile executable compliance controls only from the supplied obligations. "
            "Do not read or infer directly from source text. Return JSON matching "
            "control_draft_set.v1 with status draft. Each control must include exactly "
            "control_id, module_id, title, severity, obligation_ids, "
            "evidence_claims, evidence_requirements, missing_evidence_policy, and "
            "reuse_invalidation_keys. Preserve obligation_ids exactly. "
            "evidence_claims must be an array of atomic claims. Each claim must include "
            "claim_id, statement, proof_route_policy, obligation_ids, and proof_routes. "
            "Each proof route must include route_id, surface, claim_to_prove, "
            "expected_evidence_strength, why_this_surface, acceptance_criteria, "
            "proof_limits, and "
            "execution_mode. evidence_requirements is only a compatibility summary "
            "for one route per surface; do not return it as a keyed object. "
            "The program derives source_refs and applicability_condition from linked "
            "obligations, so do not output those fields. Treat routes as policy-level "
            "candidate proof paths, not final app-specific requirements; Applicability "
            "selects them using the confirmed AppProfile. Attach a structured condition "
            "to a route when the route is conditional on the app's actual delivery path. Do not "
            "write tool instructions, fixed filenames, symbols, line numbers, or search "
            "queries as acceptance criteria. Each acceptance criterion must be an observable "
            "statement with a criterion_id, criterion_type, statement, and scope. Absence "
            "criteria must define the inventory scope they cover. "
            "mark a route automated when its expected_evidence_strength is runtime_proof; "
            "runtime_proof routes must use execution_mode manual_or_external. Static code "
            "routes must not claim runtime proof. Do not "
            "claim that a document proves source code "
            "or runtime."
        ),
        user_payload=payload,
        output_model=ControlDraftSetTransport,
        response_schema=response_schema,
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
        candidate_surfaces: list[str] = []
        for obligation in linked_obligations:
            assert obligation is not None
            for source_ref in obligation["source_refs"]:
                key = json.dumps(source_ref, sort_keys=True)
                if key not in source_ref_keys:
                    source_ref_keys.add(key)
                    source_refs.append(source_ref)
            # Obligation surfaces remain a legacy hint. New controls derive
            # candidate surfaces from explicit proof routes below.
        conditions = {
            json.dumps(obligation["applicability_condition"], sort_keys=True)
            for obligation in linked_obligations
            if obligation is not None
        }
        applicability_condition = (
            json.loads(next(iter(conditions)))
            if len(conditions) == 1
            else {"kind": "unknown", "reason": "linked obligations have different conditions"}
        )
        evidence_requirements: dict[str, Any] = {}
        evidence_claims: list[dict[str, Any]] = []
        for claim in draft.evidence_claims:
            unknown_claim_obligations = set(claim.obligation_ids) - set(draft.obligation_ids)
            if unknown_claim_obligations:
                raise ValueError(
                    f"control {draft.control_id} claim {claim.claim_id} references "
                    "unknown obligations: "
                    + ", ".join(sorted(unknown_claim_obligations))
                )
            claim_obligations = [
                obligation
                for obligation in linked_obligations
                if obligation is not None and obligation["obligation_id"] in claim.obligation_ids
            ]
            claim_source_refs = [
                source_ref
                for obligation in claim_obligations
                for source_ref in obligation["source_refs"]
            ]
            claim_payload = claim.model_dump(mode="json")
            claim_payload["source_refs"] = claim_source_refs or source_refs
            evidence_claims.append(claim_payload)
            for route in claim.proof_routes:
                if route.surface not in candidate_surfaces:
                    candidate_surfaces.append(route.surface)
                if route.surface not in evidence_requirements:
                    evidence_requirements[route.surface] = {
                        "requirement_id": f"req.{draft.control_id}.{route.surface}",
                        "minimum_strength": route.expected_evidence_strength,
                        "rationale": route.claim_to_prove,
                        "obligation_ids": list(claim.obligation_ids),
                        "source_refs": claim_source_refs or source_refs,
                        "condition": (
                            route.condition.model_dump(mode="json")
                            if route.condition is not None
                            else None
                        ),
                    }
        if not draft.evidence_claims:
            for obligation in linked_obligations:
                assert obligation is not None
                for surface in obligation["required_surfaces"]:
                    if surface not in candidate_surfaces:
                        candidate_surfaces.append(surface)
        for item in draft.evidence_requirements:
            if draft.evidence_claims and item.surface not in candidate_surfaces:
                # Some providers emit the legacy summary alongside Claims.
                # It must not expand the route-derived review denominator.
                continue
            if item.surface in evidence_requirements:
                if draft.evidence_claims:
                    # The transport may include a richer compatibility
                    # summary alongside the route-derived requirement.
                    continue
                raise ValueError(
                    f"control {draft.control_id} has duplicate evidence surface: "
                    f"{item.surface}"
                )
            evidence_requirements[item.surface] = {
                "requirement_id": f"req.{draft.control_id}.{item.surface}",
                "minimum_strength": item.minimum_strength,
                "rationale": item.rationale,
                "obligation_ids": [
                    obligation["obligation_id"]
                    for obligation in linked_obligations
                    if obligation is not None and item.surface in obligation["required_surfaces"]
                ],
                "source_refs": [
                    source_ref
                    for obligation in linked_obligations
                    if obligation is not None and item.surface in obligation["required_surfaces"]
                    for source_ref in obligation["source_refs"]
                ],
                "condition": (
                    item.condition.model_dump(mode="json")
                    if item.condition is not None
                    else None
                ),
            }
        controls.append(
            ControlDraft.model_validate(
                {
                    **draft.model_dump(exclude={"evidence_requirements"}),
                    "source_refs": source_refs,
                    "applicability_condition": applicability_condition,
                    "candidate_surfaces": candidate_surfaces,
                    # Keep the old serialized field until downstream readers
                    # complete the migration. Runtime planning uses candidates.
                    "required_surfaces": candidate_surfaces,
                    "evidence_requirements": evidence_requirements,
                    "evidence_claims": evidence_claims,
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
