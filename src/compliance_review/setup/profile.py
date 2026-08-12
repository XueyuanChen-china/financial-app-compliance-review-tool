from __future__ import annotations

import json
from typing import Any, Mapping, TypedDict

from langgraph.graph import END, START, StateGraph

from compliance_review.domain.models import WorkItem
from compliance_review.repository import RepositorySandbox
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
)


def build_profile_draft(
    inventories: list[RepositoryInventory], facts: AppFactSet
) -> AppProfile:
    surfaces = sorted(
        {surface for inventory in inventories for surface in inventory.detected_surfaces}
    )
    roots = {
        surface: next(
            inventory.path
            for inventory in inventories
            if surface in inventory.detected_surfaces
        )
        for surface in surfaces
    }
    evidence = [
        ProfileEvidence(fact_id=fact.fact_id, summary=fact.fact_type)
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
    }
    required = {"app_name", "package_name", "jurisdiction", "business_type", "self_lending"}
    status: ProfileStatus = "awaiting_confirmation" if required else "draft"
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

    inventory: dict[str, Any]
    facts: dict[str, Any]
    work_item: dict[str, Any]
    agent_id: str
    attempt_id: str
    messages: list[dict[str, Any]]
    response: dict[str, Any]
    tool_rounds: int
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
        surface = inventory.detected_surface or inventory.declared_surface or "other_external"
        work_item = WorkItem(
            work_item_id=f"profile.{inventory.repo_id}",
            module_id="profile_intake",
            surface=surface,
            control_ids=["profile"],
            allowed_roots=["."],
            max_tool_rounds=self.max_rounds,
            max_files_read=20,
            max_lines_per_read=300,
        )
        graph = _build_profile_agent_graph(
            provider=self.provider,
            inventory=inventory,
            facts=facts,
            sandbox=sandbox,
            work_item=work_item,
            max_rounds=self.max_rounds,
        )
        result = graph.invoke(
            {
                "inventory": inventory.model_dump(mode="json"),
                "facts": facts.model_dump(mode="json"),
                "work_item": work_item.model_dump(mode="json"),
                "agent_id": agent_id,
                "attempt_id": f"profile.{inventory.repo_id}",
                "tool_rounds": 0,
            }
        )
        if "profile" not in result:
            raise RuntimeError(result.get("error", "profile agent did not produce a profile"))
        return AppProfile.model_validate(result["profile"])


def _build_profile_agent_graph(
    provider: ModelProvider,
    inventory: RepositoryInventory,
    facts: AppFactSet,
    sandbox: RepositorySandbox,
    work_item: WorkItem,
    max_rounds: int,
) -> Any:
    """Build the isolated Profile Agent subgraph.

    The graph owns the model/tool loop, while inventory, facts, and the sandbox
    remain injected dependencies rather than writable graph state.
    """

    def initialize(state: ProfileAgentState) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Build an AppProfile JSON draft from the supplied inventory and facts. "
                        "Use unknown/unresolved when code cannot prove a business fact. "
                        "Return only the AppProfile object when finished."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"inventory": inventory.model_dump(), "facts": facts.model_dump()},
                        sort_keys=True,
                    ),
                },
            ],
            "tool_rounds": 0,
        }

    def call_model(state: ProfileAgentState) -> dict[str, Any]:
        try:
            response = provider.complete(
                ModelRequest(
                    work_item=work_item,
                    attempt_id=state["attempt_id"],
                    agent_id=state["agent_id"],
                    messages=state["messages"],
                    tools=_profile_tool_schemas(),
                    token_budget=4000,
                )
            )
            return {"response": response.model_dump()}
        except Exception as exc:
            return {"error": str(exc)}

    def execute_tools(state: ProfileAgentState) -> dict[str, Any]:
        try:
            tool_rounds = state.get("tool_rounds", 0) + 1
            if tool_rounds > max_rounds:
                raise RuntimeError("profile agent exceeded max_rounds")
            response = ModelResponse.model_validate(state["response"])
            executor = ScopedToolExecutor(sandbox, work_item)
            messages = list(state.get("messages", []))
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, sort_keys=True),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                if call.name == "get_repository_inventory":
                    content = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
                elif call.name == "get_app_facts":
                    content = json.dumps(facts.model_dump(mode="json"), sort_keys=True)
                else:
                    content = serialize_tool_result(executor.execute(call))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": content,
                    }
                )
            return {"messages": messages, "tool_rounds": tool_rounds}
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


def _profile_tool_schemas() -> list[dict[str, Any]]:
    return [
        *tool_schemas(),
        {
            "type": "function",
            "function": {
                "name": "get_repository_inventory",
                "description": "Read the deterministic inventory for the assigned repository.",
                "parameters": {"type": "object", "properties": {}},
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
