from __future__ import annotations

import json
from typing import Any, Mapping, TypedDict

from langgraph.graph import END, START, StateGraph

from compliance_review.domain.models import WorkItem
from compliance_review.repository import RepositorySandbox
from compliance_review.review.context import (
    AgentRound,
    CompressedReviewMemory,
    ContextBudgetExceeded,
    ReviewerContextManager,
    ReviewerContextState,
    ReviewerRuntimeConfig,
)
from compliance_review.review.models import ModelRequest, ModelResponse
from compliance_review.review.provider import ModelProvider, tool_schemas
from compliance_review.review.tools import ScopedToolExecutor, serialize_tool_result
from compliance_review.setup.models import (
    AppFactSet,
    AppProfile,
    AppProfileField,
    ProfileEvidence,
    ProfileStatus,
    ProfileValidationResult,
    RepositoryInventory,
    WorkspaceMaterial,
)


def build_profile_draft(
    inventories: list[RepositoryInventory],
    facts: AppFactSet,
    materials: list[WorkspaceMaterial] | None = None,
) -> AppProfile:
    materials = materials or []
    surfaces = sorted(
        {
            *(
                surface
                for inventory in inventories
                for surface in inventory.detected_surfaces
            ),
            *(material.surface for material in materials if material.surface is not None),
        }
    )
    roots = {
        surface: [
            inventory.path
            for inventory in inventories
            if surface in inventory.detected_surfaces
        ]
        for surface in surfaces
    }
    material_roots: dict[str, list[str]] = {}
    for material in materials:
        if material.surface is not None:
            material_roots.setdefault(material.surface, []).append(material.path)
    evidence = [
        ProfileEvidence(
            repo_id=fact.repo_id,
            fact_id=fact.fact_id,
            summary=fact.fact_type,
        )
        for fact in facts.facts
        if fact.fact_type == "repository_surface"
    ]
    fields = {
        "app_name": AppProfileField(value=None, source="unresolved", confidence="low"),
        "package_name": AppProfileField(value=None, source="unresolved", confidence="low"),
        "jurisdiction": AppProfileField(value=None, source="unresolved", confidence="low"),
        "business_type": AppProfileField(value=None, source="unresolved", confidence="low"),
        "self_lending": AppProfileField(value="unknown", source="unresolved", confidence="low"),
        "evidence_surfaces": AppProfileField(
            value=surfaces,
            source="deterministic",
            confidence="high" if surfaces else "low",
            evidence=evidence,
        ),
        "review_scope": AppProfileField(
            value="multi_surface_static_review" if surfaces else "partial",
            source="deterministic",
            confidence="high" if surfaces else "low",
        ),
        "repository_roots": AppProfileField(
            value=roots,
            source="deterministic",
            confidence="high",
        ),
        "material_roots": AppProfileField(
            value=material_roots,
            source="deterministic",
            confidence="high" if material_roots else "low",
        ),
    }
    # Profile is intentionally provisional.  Business/legal questions are
    # deferred to the policy-aware Applicability Resolution Loop.
    status: ProfileStatus = "draft"
    return AppProfile(version="1.0", status=status, fields=fields)


class ProfileValidator:
    required_fields = {"app_name", "package_name", "jurisdiction", "business_type", "self_lending"}

    def validate(
        self,
        profile: AppProfile,
        inventories: list[RepositoryInventory],
        facts: AppFactSet,
    ) -> ProfileValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        conflicts: list[str] = []
        for field_name in self.required_fields:
            field = profile.fields.get(field_name)
            if field is None:
                errors.append(f"missing profile field: {field_name}")
            elif field.value is None or field.value == "unknown":
                warnings.append(f"profile field unresolved: {field_name}")
        surface_field = profile.fields.get("evidence_surfaces")
        if surface_field is None or not isinstance(surface_field.value, list):
            errors.append("evidence_surfaces must be a list")
        detected = {surface for inventory in inventories for surface in inventory.detected_surfaces}
        declared = {
            inventory.declared_surface
            for inventory in inventories
            if inventory.declared_surface is not None
        }
        selected = set(surface_field.value or []) if surface_field else set()
        if not detected.issubset(selected):
            warnings.append("profile evidence_surfaces does not include every detected surface")
        if declared and not declared.issubset(selected):
            conflicts.append("declared surface is missing from profile evidence_surfaces")
        known_fact_ids = {fact.fact_id for fact in facts.facts}
        for field_name, field in profile.fields.items():
            for evidence in field.evidence:
                if evidence.fact_id is not None and evidence.fact_id not in known_fact_ids:
                    errors.append(
                        f"profile field {field_name} references unknown fact: {evidence.fact_id}"
                    )
        conflicts.extend(
            f"repository surface unresolved: {inventory.repo_id}"
            for inventory in inventories
            if inventory.surface_status == "unresolved" and inventory.declared_surface is not None
        )
        return ProfileValidationResult(
            valid=not errors and not conflicts,
            errors=errors,
            warnings=warnings,
            conflicts=conflicts,
        )


class ProfileAgentState(TypedDict, total=False):
    """Serializable state for one Profile Agent subgraph invocation."""

    inventories: list[dict[str, Any]]
    facts: dict[str, Any]
    work_item: dict[str, Any]
    agent_id: str
    attempt_id: str
    messages: list[dict[str, Any]]
    context: dict[str, Any]
    response: dict[str, Any]
    tool_rounds: int
    tokens_used: int
    profile: dict[str, Any]
    error: str


class ProfileAgent:
    """Run bounded, read-only AppProfile inference as a LangGraph subgraph."""

    def __init__(self, provider: ModelProvider, max_rounds: int = 4) -> None:
        self.provider = provider
        self.max_rounds = max_rounds

    def run(
        self,
        inventory: RepositoryInventory,
        facts: AppFactSet,
        sandbox: RepositorySandbox,
        agent_id: str = "profile-agent-001",
    ) -> AppProfile:
        return self.run_workspace(
            [inventory],
            facts,
            {inventory.repo_id: sandbox},
            agent_id=agent_id,
        )

    def run_workspace(
        self,
        inventories: list[RepositoryInventory],
        facts: AppFactSet,
        sandboxes: dict[str, RepositorySandbox],
        agent_id: str = "profile-agent-001",
    ) -> AppProfile:
        if not inventories:
            raise ValueError("at least one repository inventory is required")
        missing = {
            inventory.repo_id for inventory in inventories
        } - set(sandboxes)
        if missing:
            raise ValueError(f"missing repository sandboxes: {sorted(missing)}")
        first_surface = (
            inventories[0].detected_surface
            or inventories[0].declared_surface
            or "other_external"
        )
        work_item = WorkItem(
            work_item_type="profile_discovery",
            work_item_id="profile.workspace",
            module_id="profile_intake",
            surface=first_surface,
            control_ids=["profile"],
            allowed_roots=["."],
            max_tool_rounds=self.max_rounds,
            max_files_read=20,
            max_lines_per_read=300,
        )
        graph = _build_profile_agent_graph(
            provider=self.provider,
            inventories=inventories,
            facts=facts,
            sandboxes=sandboxes,
            work_item=work_item,
            max_rounds=self.max_rounds,
        )
        result = graph.invoke(
            {
                "inventories": [
                    inventory.model_dump(mode="json") for inventory in inventories
                ],
                "facts": facts.model_dump(mode="json"),
                "work_item": work_item.model_dump(mode="json"),
                "agent_id": agent_id,
                "attempt_id": "profile.workspace",
                "tool_rounds": 0,
                "tokens_used": 0,
            }
        )
        if "profile" not in result:
            raise RuntimeError(result.get("error", "profile agent did not produce a profile"))
        return AppProfile.model_validate(result["profile"])


def merge_profile_candidate(base: AppProfile, candidate: AppProfile) -> AppProfile:
    """Merge model proposals without allowing them to overwrite deterministic fields."""
    fields = dict(base.fields)
    protected_sources = {"declared", "deterministic", "human_confirmed"}
    for field_name, proposed in candidate.fields.items():
        existing = fields.get(field_name)
        if existing is not None and existing.source in protected_sources:
            continue
        if proposed.value is None or proposed.value == "unknown":
            fields[field_name] = proposed.model_copy(
                update={"source": "unresolved", "confidence": "low"}
            )
        else:
            fields[field_name] = proposed.model_copy(
                update={"source": "inferred", "confidence": proposed.confidence}
            )
    return base.model_copy(update={"status": "draft", "fields": fields})


def _build_profile_agent_graph(
    provider: ModelProvider,
    inventories: list[RepositoryInventory],
    facts: AppFactSet,
    sandboxes: dict[str, RepositorySandbox],
    work_item: WorkItem,
    max_rounds: int,
) -> Any:
    """Build the isolated Profile Agent subgraph.

    The graph owns the model/tool loop, while inventory, facts, and the sandbox
    remain injected dependencies rather than writable graph state.
    """

    context_manager = ReviewerContextManager(
        ReviewerRuntimeConfig(active_window_size=3, context_window_tokens=8000)
    )
    executors = {
        inventory.repo_id: ScopedToolExecutor(
            sandboxes[inventory.repo_id],
            work_item.model_copy(
                update={
                    "work_item_id": f"profile.{inventory.repo_id}",
                    "surface": (
                        inventory.detected_surface
                        or inventory.declared_surface
                        or "other_external"
                    ),
                }
            ),
        )
        for inventory in inventories
    }

    def initialize(state: ProfileAgentState) -> dict[str, Any]:
        context = context_manager.create(
            work_item,
            (
                "Build one AppProfile JSON draft for the complete workspace. Use the "
                "repository inventory and deterministic facts as authoritative technical "
                "inputs. Use unknown/unresolved when code cannot prove a business fact. "
                "Return only the AppProfile object when finished."
            ),
        )
        return {
            "context": context.model_dump(),
            "tool_rounds": 0,
        }

    def call_model(state: ProfileAgentState) -> dict[str, Any]:
        try:
            current_context = context_manager.create(
                work_item,
                "Build an AppProfile JSON draft from workspace inventory and facts.",
            )
            if state.get("context"):
                current_context = ReviewerContextState.model_validate(state["context"])
            messages = context_manager.render_messages(current_context)
            messages.insert(
                2,
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "inventories": [
                                inventory.model_dump() for inventory in inventories
                            ],
                            "facts": facts.model_dump(),
                        },
                        sort_keys=True,
                    ),
                },
            )
            compression_tokens = 0

            def compress(
                memory: CompressedReviewMemory | None,
                retired_rounds: list[AgentRound],
            ) -> CompressedReviewMemory:
                nonlocal compression_tokens
                payload = {
                    "compressed_memory": memory.model_dump() if memory else None,
                    "retired_rounds": [item.model_dump() for item in retired_rounds],
                }
                response = provider.complete(
                    ModelRequest(
                        work_item=work_item,
                        attempt_id=state["attempt_id"],
                        agent_id=state["agent_id"],
                        request_kind="compression",
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Compress only the supplied Profile Agent exploration "
                                    "rounds into a CompressedReviewMemory JSON object. "
                                    "Do not invent facts."
                                ),
                            },
                            {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                        ],
                        tools=[],
                        token_budget=4000,
                    )
                )
                compression_tokens += (
                    response.input_tokens
                    + response.output_tokens
                    + _approx_tokens(response.content)
                )
                if response.tool_calls or not response.content:
                    raise ValueError("profile compression response must be structured JSON")
                parsed = dict(_parse_json(response.content))
                parsed["generation"] = memory.generation + 1 if memory is not None else 1
                return CompressedReviewMemory.model_validate(parsed)

            prepared_context, prepared_messages = context_manager.prepare_for_model(
                current_context, messages, compress
            )
            remaining_tokens = 4000 - state.get("tokens_used", 0) - compression_tokens
            if remaining_tokens < 100:
                raise ContextBudgetExceeded("profile context budget exceeded")
            response = provider.complete(
                ModelRequest(
                    work_item=work_item,
                    attempt_id=state["attempt_id"],
                    agent_id=state["agent_id"],
                    messages=prepared_messages,
                    tools=_profile_tool_schemas(),
                    token_budget=remaining_tokens,
                )
            )
            tokens_used = (
                state.get("tokens_used", 0)
                + compression_tokens
                + response.input_tokens
                + response.output_tokens
                + _approx_tokens(response.content)
            )
            if tokens_used > 4000:
                raise ContextBudgetExceeded("profile token budget exceeded")
            updates: dict[str, Any] = {
                "response": response.model_dump(),
                "messages": prepared_messages,
                "tokens_used": tokens_used,
            }
            if not response.tool_calls:
                round_item = AgentRound(
                    round_number=len(current_context.active_rounds)
                    + len(current_context.retired_rounds)
                    + 1,
                    model_response={"content": response.content},
                    estimated_tokens=response.input_tokens + response.output_tokens,
                )
                updates["context"] = context_manager.record_round(
                    prepared_context, round_item
                ).model_dump()
            return updates
        except ContextBudgetExceeded as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": str(exc)}

    def execute_tools(state: ProfileAgentState) -> dict[str, Any]:
        try:
            tool_rounds = state.get("tool_rounds", 0) + 1
            if tool_rounds > max_rounds:
                raise RuntimeError("profile agent exceeded max_rounds")
            response = ModelResponse.model_validate(state["response"])
            current_context = ReviewerContextState.model_validate(state["context"])
            tool_calls = []
            tool_results = []
            for call in response.tool_calls:
                if call.name == "get_repository_inventory":
                    repo_id = call.arguments.get("repo_id")
                    selected = inventories
                    if isinstance(repo_id, str):
                        selected = [item for item in inventories if item.repo_id == repo_id]
                    payload = [item.model_dump(mode="json") for item in selected]
                    content = json.dumps(
                        payload[0] if isinstance(repo_id, str) and payload else payload,
                        sort_keys=True,
                    )
                elif call.name == "get_app_facts":
                    content = json.dumps(facts.model_dump(mode="json"), sort_keys=True)
                else:
                    repo_id = call.arguments.get("repo_id")
                    if not isinstance(repo_id, str) or repo_id not in executors:
                        raise ValueError("profile code tools require a valid repo_id")
                    result = executors[repo_id].execute(call)
                    content = serialize_tool_result(result)
                tool_calls.append(
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, sort_keys=True),
                        },
                    }
                )
                tool_results.append({"call_id": call.call_id, "content": content})
            round_item = AgentRound(
                round_number=len(current_context.active_rounds)
                + len(current_context.retired_rounds)
                + 1,
                model_response={"tool_calls": tool_calls},
                tool_calls=[call.model_dump() for call in response.tool_calls],
                tool_results=tool_results,
            )
            next_context = context_manager.record_round(current_context, round_item)
            return {
                "context": next_context.model_dump(),
                "messages": context_manager.render_messages(next_context),
                "tool_rounds": tool_rounds,
                "tokens_used": state.get("tokens_used", 0)
                + sum(_approx_tokens(item.get("content")) for item in tool_results),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def finalize(state: ProfileAgentState) -> dict[str, Any]:
        if state.get("error"):
            raise RuntimeError(state["error"])
        response = ModelResponse.model_validate(state.get("response", {}))
        if response.tool_calls or not response.content:
            raise ValueError("profile agent returned no final content")
        profile = AppProfile.model_validate(_parse_json(response.content))
        return {"profile": profile.model_dump(mode="json")}

    def route_after_model(state: ProfileAgentState) -> str:
        if state.get("error"):
            return "finalize"
        response = ModelResponse.model_validate(state.get("response", {}))
        return "execute_tools" if response.tool_calls else "finalize"

    builder = StateGraph(ProfileAgentState)
    builder.add_node("initialize", initialize)
    builder.add_node("call_model", call_model)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "call_model")
    builder.add_conditional_edges(
        "call_model",
        route_after_model,
        {"execute_tools": "execute_tools", "finalize": "finalize"},
    )
    builder.add_edge("execute_tools", "call_model")
    builder.add_edge("finalize", END)
    return builder.compile()


def _parse_json(content: str) -> Mapping[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("profile agent output must be a JSON object")
    return value


def _approx_tokens(value: Any) -> int:
    return max(0, (len(str(value)) + 3) // 4)


def _profile_tool_schemas() -> list[dict[str, Any]]:
    schemas = [*tool_schemas()]
    for schema in schemas:
        function = schema.get("function", {})
        name = function.get("name")
        if name in {
            "code_map_query",
            "code_map_path",
            "code_map_explain",
            "code_map_callers",
            "code_map_callees",
            "code_map_impact",
            "list_files",
            "search_code",
            "read_file",
        }:
            properties = function.setdefault("parameters", {}).setdefault("properties", {})
            properties["repo_id"] = {
                "type": "string",
                "description": (
                    "Repository id to inspect; required when the workspace has "
                    "multiple repositories."
                ),
            }
    return [
        *schemas,
        {
            "type": "function",
            "function": {
                "name": "get_repository_inventory",
                "description": "Read the deterministic inventory for the assigned repository.",
                "parameters": {
                    "type": "object",
                    "properties": {"repo_id": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_app_facts",
                "description": "Read deterministic AppFacts collected for the assigned repository.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
