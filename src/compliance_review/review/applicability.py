from __future__ import annotations

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence, TypedDict

from langgraph.graph import END, START, StateGraph

from compliance_review.collectors.base import CollectorResult
from compliance_review.compilation.models import Obligation, SourceRegistry
from compliance_review.domain.models import (
    ApplicabilityCondition,
    ApplicabilityDecision,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    ApplicabilityQuestion,
    ApplicabilityResolution,
    ApplicabilitySet,
    ApplicabilityValidationIssue,
    ContractModel,
    Control,
    ControlSet,
    Fact,
    ProfileFactRef,
    SourceRef,
    Surface,
    SurfaceRequirementDecision,
    SurfaceRequirementStatus,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.models import ModelRequest, ModelResponse, ScopedToolResult, ToolCall
from compliance_review.review.provider import ModelProvider, tool_schemas
from compliance_review.review.tools import ScopedToolExecutor, serialize_tool_result

if TYPE_CHECKING:
    from compliance_review.setup.models import AppFactSet, RepositoryInventory

_EQUALS_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<value>.+)$")
_INCLUDES_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+includes\s+(?P<value>.+)$")
_IN_RE = re.compile(r"^(?P<value>[A-Za-z0-9_.-]+)\s+in\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)$")


class SemanticApplicabilityResponse(ContractModel):
    """Top-level model contract retains Pydantic $defs for nested references."""

    decisions: list[ApplicabilityDecision]


class _ApplicabilityLoopState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    response: dict[str, Any]
    decision: dict[str, Any]
    errors: list[str]
    tool_rounds: int
    tool_calls: int
    attempts: int


class _ApplicabilityToolRouter:
    """Route read-only applicability tool calls to a surface-bounded executor."""

    def __init__(
        self,
        executors: Mapping[str, list[ScopedToolExecutor]],
        default_surface: str,
    ) -> None:
        self.executors = {surface: list(items) for surface, items in executors.items()}
        self.default_surface = default_surface
        self.tool_calls = 0

    def execute(self, call: ToolCall) -> ScopedToolResult:
        requested_surface = call.arguments.get("surface", self.default_surface)
        candidates = self.executors.get(str(requested_surface), [])
        if not candidates:
            return ScopedToolResult(
                call_id=call.call_id,
                name=call.name,
                ok=False,
                error=f"surface is not available to Applicability: {requested_surface}",
                error_code="surface_unavailable",
                retryable=False,
            )
        requested_repository = call.arguments.get("repository_id")
        if requested_repository is not None and not isinstance(requested_repository, str):
            return ScopedToolResult(
                call_id=call.call_id,
                name=call.name,
                ok=False,
                error="repository_id must be a string",
                error_code="invalid_argument",
                retryable=False,
            )
        if requested_repository:
            candidates = [
                item
                for item in candidates
                if item.work_item.repository_id == requested_repository
            ]
            if not candidates:
                return ScopedToolResult(
                    call_id=call.call_id,
                    name=call.name,
                    ok=False,
                    error=f"repository is not available for Applicability: {requested_repository}",
                    error_code="repository_unavailable",
                    retryable=False,
                )
        elif len(candidates) > 1:
            return ScopedToolResult(
                call_id=call.call_id,
                name=call.name,
                ok=False,
                error=(
                    "repository_id is required when multiple repositories share the "
                    f"surface: {requested_surface}"
                ),
                error_code="repository_required",
                retryable=True,
            )
        self.tool_calls += 1
        return candidates[0].execute(call)

    def repository_summary(self) -> list[dict[str, str]]:
        return [
            {"repository_id": item.work_item.repository_id, "surface": surface}
            for surface, items in sorted(self.executors.items())
            for item in items
        ]


class ApplicabilityResolutionLoop:
    """Checkpointable-style bounded Applicability Agent Loop.

    The loop uses the same read-only tool contract as Reviewer, but produces
    applicability decisions only.  Human answers are optional; when omitted,
    unresolved facts become durable pending questions instead of being guessed.
    """

    MAX_CONCURRENCY = 3

    def __init__(
        self,
        provider: ModelProvider | None,
        source_registry: SourceRegistry | None = None,
        obligations: list[Obligation] | None = None,
        max_tool_rounds: int = 6,
        max_validation_retries: int = 2,
        max_concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        if not 1 <= max_concurrency <= self.MAX_CONCURRENCY:
            raise ValueError(
                f"Applicability max_concurrency must be between 1 and {self.MAX_CONCURRENCY}"
            )
        self.provider = provider
        self.source_registry = source_registry
        self.obligations = obligations or []
        self.max_tool_rounds = max_tool_rounds
        self.max_validation_retries = max_validation_retries
        self.max_concurrency = max_concurrency

    def resolve(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        inventories: list[RepositoryInventory],
        app_facts: AppFactSet,
        human_answers: Mapping[str, Any] | None = None,
        initial_decisions: Sequence[ApplicabilityDecision] = (),
        checkpoint_callback: Callable[
            [list[ApplicabilityDecision], int, int], None
        ] | None = None,
        initial_attempts: int = 0,
        initial_tool_calls: int = 0,
    ) -> tuple[ApplicabilityProfile, ApplicabilitySet, ApplicabilityResolution]:
        self._validate_human_answers(controls, human_answers or {})
        effective_profile = self._apply_human_answers(profile, human_answers or {})
        controls_by_id = {control.control_id: control for control in controls.controls}
        initial_by_id = {decision.control_id: decision for decision in initial_decisions}
        unknown_checkpoint_ids = sorted(set(initial_by_id) - set(controls_by_id))
        if unknown_checkpoint_ids:
            raise ValueError(
                "applicability checkpoint contains unknown Control ids: "
                + ", ".join(unknown_checkpoint_ids)
            )
        decisions: list[ApplicabilityDecision] = []
        issues: list[str] = []
        questions: dict[str, ApplicabilityQuestion] = {}
        attempts = initial_attempts

        completed: dict[str, ApplicabilityDecision] = {
            control_id: decision
            for control_id, decision in initial_by_id.items()
        }
        pending_controls = [
            control for control in controls.controls if control.control_id not in completed
        ]
        futures: dict[Future[tuple[ApplicabilityDecision, int, int, list[str]]], str] = {}
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            for control in pending_controls:
                future = executor.submit(
                    self._resolve_control_isolated,
                    effective_profile,
                    control,
                    inventories,
                    app_facts,
                )
                futures[future] = control.control_id

            for future in as_completed(futures):
                control_id = futures[future]
                decision, control_attempts, control_tool_calls, control_issues = future.result()
                completed[control_id] = decision
                attempts += control_attempts
                initial_tool_calls += control_tool_calls
                issues.extend(control_issues)
                decisions = [
                    completed[control.control_id]
                    for control in controls.controls
                    if control.control_id in completed
                ]
                questions = self._collect_questions(
                    controls, decisions, effective_profile
                )
                if checkpoint_callback is not None:
                    checkpoint_callback(list(decisions), attempts, initial_tool_calls)

        decisions = [completed[control.control_id] for control in controls.controls]
        questions = self._collect_questions(controls, decisions, effective_profile)
        applicability = _build_applicability_set(
            effective_profile,
            controls,
            decisions,
        )
        pending_questions = list(questions.values())
        has_unknown_decisions = any(item.decision == "unknown" for item in decisions)
        if pending_questions:
            status: Literal["complete", "awaiting_human", "partial"] = "awaiting_human"
        elif has_unknown_decisions or issues:
            status = "partial"
        else:
            status = "complete"
        resolution = ApplicabilityResolution(
            profile_version=effective_profile.version,
            control_version=controls.version,
            status=status,
            decisions=decisions,
            pending_questions=pending_questions,
            validation_issues=[
                _issue_from_text(text, decisions) for text in sorted(set(issues))
            ],
            attempts=attempts,
            tool_calls=initial_tool_calls,
        )
        return effective_profile, applicability, resolution

    def _resolve_control_isolated(
        self,
        profile: ApplicabilityProfile,
        control: Control,
        inventories: list[RepositoryInventory],
        app_facts: AppFactSet,
    ) -> tuple[ApplicabilityDecision, int, int, list[str]]:
        """Resolve one Control with a private read-only router for parallel safety."""
        router = self._build_tool_router(inventories, app_facts)
        decision, attempts, issues = self._resolve_control(
            profile,
            control,
            router,
            app_facts.facts,
        )
        return decision, attempts, router.tool_calls, issues

    def _collect_questions(
        self,
        controls: ControlSet,
        decisions: Sequence[ApplicabilityDecision],
        profile: ApplicabilityProfile,
    ) -> dict[str, ApplicabilityQuestion]:
        decisions_by_id = {decision.control_id: decision for decision in decisions}
        questions: dict[str, ApplicabilityQuestion] = {}
        for control in controls.controls:
            decision = decisions_by_id.get(control.control_id)
            if decision is None:
                continue
            for question in self._questions_for(control, decision, profile):
                existing = questions.get(question.fact_key)
                if existing is None:
                    questions[question.fact_key] = question
                else:
                    questions[question.fact_key] = existing.model_copy(
                        update={
                            "affected_control_ids": sorted(
                                set(existing.affected_control_ids + question.affected_control_ids)
                            )
                        }
                    )
        return questions

    def _resolve_control(
        self,
        profile: ApplicabilityProfile,
        control: Control,
        router: _ApplicabilityToolRouter,
        facts: list[Fact],
    ) -> tuple[ApplicabilityDecision, int, list[str]]:
        if self.provider is None:
            # The new main path must not silently fall back to the legacy
            # expression hint. Without a model response, preserve the Control
            # in the denominator and let the normal unknown/work-item path
            # handle it instead of making an unreviewed applicability claim.
            return (
                ApplicabilityDecision(
                    control_id=control.control_id,
                    decision="unknown",
                    reason="applicability provider is unavailable",
                    source_refs=control.source_refs,
                    surface_requirements=_legacy_surface_requirements(control),
                    unresolved_conditions=["applicability_provider_unavailable"],
                    confidence="low",
                ),
                0,
                ["applicability provider is unavailable"],
            )
        work_item = WorkItem(
            work_item_type="applicability_resolution",
            work_item_id=f"applicability.{control.control_id}",
            module_id="applicability",
            surface=control.surface_candidates[0],
            control_ids=[control.control_id],
            allowed_roots=["."],
            max_tool_rounds=self.max_tool_rounds,
            max_files_read=20,
            max_lines_per_read=300,
        )
        obligations_by_id = {
            obligation.obligation_id: obligation for obligation in self.obligations
        }
        allowed_source_refs = _allowed_source_refs(control, obligations_by_id)
        known_technical_fact_ids = sorted(fact.fact_id for fact in facts)
        initial = [
            {
                "role": "system",
                "content": (
                    "You are the Applicability Agent. Decide only the supplied Control. "
                    "Use read-only tools when technical facts can reduce uncertainty. "
                    "Technical presence does not prove licensing or legal status. "
                    "Never change required_evidence_surfaces. Return one JSON "
                    "ApplicabilityDecision. "
                    "Copy source_refs exactly from the supplied allowed_source_refs. "
                    "technical_fact_refs may contain only IDs from known_technical_fact_ids. "
                    "Never put a file path, symbol, search description, or tool output text "
                    "into technical_fact_refs; those are investigation hints, not fact IDs. "
                    "not_applicable requires valid source_refs and confirmed profile_fact_refs. "
                    "If a fact remains unresolved, return unknown and list unresolved_conditions."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "confirmed_profile_facts": {
                            name: fact.model_dump(mode="json")
                            for name, fact in profile.confirmed_facts.items()
                        },
                        "allowed_profile_fact_refs": [
                            {
                                "field_name": name,
                                "expected_value": _canonical_json(fact.value),
                                "source": fact.source,
                            }
                            for name, fact in sorted(profile.confirmed_facts.items())
                        ],
                        "available_repositories": router.repository_summary(),
                        "allowed_source_refs": [
                            item.model_dump(mode="json") for item in allowed_source_refs
                        ],
                        "known_technical_fact_ids": known_technical_fact_ids,
                        "known_technical_facts": [
                            {
                                "fact_id": fact.fact_id,
                                "fact_type": fact.fact_type,
                                "observed_value": fact.observed_value,
                                "source_refs": [
                                    item.model_dump(mode="json") for item in fact.source_refs
                                ],
                            }
                            for fact in facts
                        ],
                        "control": {
                            "control_id": control.control_id,
                            "title": control.title,
                            "candidate_surfaces": control.surface_candidates,
                            "applicability_condition": control.applicability_condition.model_dump(
                                mode="json"
                            ),
                            "source_refs": [
                                item.model_dump(mode="json") for item in control.source_refs
                            ],
                            "obligations": _obligation_context(
                                control, self.obligations, self.source_registry
                            ),
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        graph = self._build_control_graph(profile, control, work_item, router, facts)
        result = graph.invoke({"messages": initial, "tool_rounds": 0, "attempts": 0})
        raw_decision = result.get("decision")
        if not raw_decision:
            return (
                ApplicabilityDecision(
                    control_id=control.control_id,
                    decision="unknown",
                    reason="Applicability Agent did not produce a valid decision",
                    source_refs=control.source_refs,
                    unresolved_conditions=["applicability_agent_no_valid_output"],
                    confidence="low",
                ),
                int(result.get("attempts", 0)),
                list(result.get("errors", [])),
            )
        return (
            ApplicabilityDecision.model_validate(raw_decision),
            int(result.get("attempts", 0)),
            list(result.get("errors", [])),
        )

    def _build_control_graph(
        self,
        profile: ApplicabilityProfile,
        control: Control,
        work_item: WorkItem,
        router: _ApplicabilityToolRouter,
        facts: list[Fact],
    ) -> Any:
        validator = ApplicabilityValidator()

        def call_model(state: _ApplicabilityLoopState) -> dict[str, Any]:
            if self.provider is None:
                return {"errors": ["applicability provider is unavailable"]}
            try:
                response = self.provider.complete(
                    ModelRequest(
                        work_item=work_item,
                        attempt_id=f"applicability.{control.control_id}",
                        agent_id="applicability-agent",
                        request_kind="applicability",
                        messages=state["messages"],
                        tools=tool_schemas(),
                        response_schema=ApplicabilityDecision.model_json_schema(),
                        token_budget=12_000,
                    )
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors = list(state.get("errors", []))
                errors.append(f"applicability provider failed: {exc}")
                return {
                    "response": ModelResponse().model_dump(),
                    "attempts": state.get("attempts", 0) + 1,
                    "errors": errors,
                }
            return {
                "response": response.model_dump(),
                "attempts": state.get("attempts", 0) + 1,
            }

        def execute_tools(state: _ApplicabilityLoopState) -> dict[str, Any]:
            response = ModelResponse.model_validate(state["response"])
            messages = list(state["messages"])
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
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
            results: list[ScopedToolResult] = []
            for call in response.tool_calls:
                result = router.execute(call)
                results.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": serialize_tool_result(result),
                    }
                )
            rounds = state.get("tool_rounds", 0) + 1
            if rounds > self.max_tool_rounds:
                return {
                    "messages": messages,
                    "tool_rounds": rounds,
                    "errors": ["applicability tool-round budget exhausted"],
                    "response": {},
                }
            return {"messages": messages, "tool_rounds": rounds}

        def validate(state: _ApplicabilityLoopState) -> dict[str, Any]:
            response = ModelResponse.model_validate(state.get("response", {}))
            content = response.content or ""
            try:
                payload = json.loads(content)
                if "decisions" in payload:
                    parsed = SemanticApplicabilityResponse.model_validate(payload)
                    decision = next(
                        item for item in parsed.decisions if item.control_id == control.control_id
                    )
                else:
                    decision = ApplicabilityDecision.model_validate(payload)
                normalized, errors = validator.validate_draft(
                    profile,
                    control,
                    decision,
                    obligations=self.obligations,
                    source_registry=self.source_registry,
                    facts=facts,
                )
                if errors and state.get("attempts", 0) <= self.max_validation_retries:
                    messages = list(state["messages"])
                    messages.append(
                        {
                            "role": "user",
                            "content": "Validator rejected the draft. Fix only these errors: "
                            + json.dumps(errors, ensure_ascii=False),
                        }
                    )
                    return {"messages": messages, "errors": errors, "decision": {}}
                if errors:
                    normalized = normalized.model_copy(
                        update={
                            "decision": "unknown",
                            "reason": "Applicability validator retry budget exhausted",
                            "unresolved_conditions": sorted(
                                set(
                                    normalized.unresolved_conditions
                                    + ["validator_retry_exhausted"]
                                )
                            ),
                            "confidence": "low",
                        }
                    )
                return {"decision": normalized.model_dump(mode="json"), "errors": errors}
            except (StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
                errors = [f"invalid applicability draft: {exc}"]
                if state.get("attempts", 0) <= self.max_validation_retries:
                    messages = list(state["messages"])
                    messages.append({"role": "user", "content": errors[0]})
                    return {"messages": messages, "errors": errors, "decision": {}}
                return {"errors": errors, "decision": {}}

        def route_after_model(state: _ApplicabilityLoopState) -> str:
            response = ModelResponse.model_validate(state.get("response", {}))
            if response.tool_calls and state.get("tool_rounds", 0) < self.max_tool_rounds:
                return "tools"
            return "validate"

        def route_after_validate(state: _ApplicabilityLoopState) -> str:
            if state.get("decision"):
                return END
            if state.get("attempts", 0) <= self.max_validation_retries:
                return "model"
            return END

        builder = StateGraph(_ApplicabilityLoopState)
        builder.add_node("model", call_model)
        builder.add_node("tools", execute_tools)
        builder.add_node("validate", validate)
        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model", route_after_model, {"tools": "tools", "validate": "validate"}
        )
        builder.add_edge("tools", "model")
        builder.add_conditional_edges(
            "validate", route_after_validate, {"model": "model", END: END}
        )
        return builder.compile()

    def _build_tool_router(
        self, inventories: list[RepositoryInventory], facts: AppFactSet
    ) -> _ApplicabilityToolRouter:
        collector_results = {
            f"{item.repo_id or 'workspace'}/{item.collector_id}/{index}": item
            for index, item in enumerate(
                [CollectorResult.model_validate(value) for value in facts.collector_results],
                start=1,
            )
        }
        executors: dict[str, list[ScopedToolExecutor]] = {}
        for inventory in inventories:
            surface = inventory.detected_surface or inventory.declared_surface
            if surface is None:
                continue
            relevant_fact_ids = [
                fact.fact_id
                for fact in facts.facts
                if fact.source_surface == surface and fact.repo_id in {None, inventory.repo_id}
            ]
            work_item = WorkItem(
                work_item_type="applicability_resolution",
                work_item_id=f"applicability.tools.{inventory.repo_id}",
                module_id="applicability",
                repository_id=inventory.repo_id,
                repository_ids=[inventory.repo_id],
                surface=surface,
                control_ids=["applicability"],
                collector_fact_refs=relevant_fact_ids,
                allowed_roots=["."],
                max_tool_rounds=self.max_tool_rounds,
                max_files_read=20,
                max_lines_per_read=300,
            )
            executors.setdefault(surface, []).append(
                ScopedToolExecutor(
                    RepositorySandbox(Path(inventory.path)),
                    work_item,
                    collector_results=collector_results,
                )
            )
        default_surface = next(iter(executors), "other_external")
        return _ApplicabilityToolRouter(executors, default_surface)

    @staticmethod
    def _apply_human_answers(
        profile: ApplicabilityProfile, answers: Mapping[str, Any]
    ) -> ApplicabilityProfile:
        if not answers:
            return profile
        updated = profile.model_copy(deep=True)
        facts = dict(updated.confirmed_facts)
        updates: dict[str, Any] = {}
        for fact_key, value in answers.items():
            facts[fact_key] = ApplicabilityProfileFact(value=value, source="human_confirmed")
            if fact_key == "business_type":
                updates["business_type"] = [value] if isinstance(value, str) else value
            elif fact_key == "self_lending":
                updates["self_lending"] = value
            elif fact_key == "jurisdiction":
                updates["jurisdiction"] = value
        return ApplicabilityProfile.model_validate(
            updated.model_dump(mode="python")
            | updates
            | {"confirmed_facts": facts}
        )

    @staticmethod
    def _validate_human_answers(
        controls: ControlSet, answers: Mapping[str, Any]
    ) -> None:
        if not answers:
            return
        allowed = {"business_type", "self_lending", "jurisdiction"}
        allowed.update(
            fact_key
            for control in controls.controls
            for fact_key in _condition_fact_keys(control.applicability_condition)
        )
        unexpected = sorted(set(answers) - allowed)
        if unexpected:
            raise ValueError(f"unsupported applicability answer keys: {unexpected}")
        for key, value in answers.items():
            if value is None:
                raise ValueError(f"applicability answer must not be null: {key}")
            if key == "self_lending" and not isinstance(value, bool):
                raise ValueError("self_lending applicability answer must be boolean")
            if key == "business_type":
                valid = isinstance(value, str) and bool(value.strip())
                valid = valid or (
                    isinstance(value, list)
                    and bool(value)
                    and all(isinstance(item, str) and bool(item.strip()) for item in value)
                )
                if not valid:
                    raise ValueError(
                        "business_type applicability answer must be a non-empty string or list"
                    )
            if key == "jurisdiction" and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError("jurisdiction applicability answer must be a non-empty string")

    @staticmethod
    def _questions_for(
        control: Control,
        decision: ApplicabilityDecision,
        profile: ApplicabilityProfile,
    ) -> list[ApplicabilityQuestion]:
        if decision.decision != "unknown":
            return []
        keys = _condition_fact_keys(control.applicability_condition)
        text = " ".join(decision.unresolved_conditions).lower()
        if not keys:
            if "loan" in text or "lending" in text or "business" in text:
                keys.append("business_type")
            if "self" in text or "lend" in text:
                keys.append("self_lending")
            if "jurisdiction" in text or "country" in text or "region" in text:
                keys.append("jurisdiction")
        if not keys:
            keys = [f"control_{_safe_fact_key(control.control_id)}_applicability_fact"]
        questions: list[ApplicabilityQuestion] = []
        for key in sorted(set(keys)):
            existing = profile.confirmed_facts.get(key)
            if existing is not None and existing.source in {"human_confirmed", "deterministic"}:
                continue
            questions.append(
                ApplicabilityQuestion(
                    question_id=f"aq.{_safe_key(key)}",
                    fact_key=key,
                    question=_question_text(key, control.title, decision.reason),
                    affected_control_ids=[control.control_id],
                )
            )
        return questions


class ApplicabilityValidator:
    """Verify semantic claims without trying to reinterpret policy prose."""

    def validate_draft(
        self,
        profile: ApplicabilityProfile,
        control: Control,
        decision: ApplicabilityDecision,
        obligations: list[Obligation] | None = None,
        source_registry: SourceRegistry | None = None,
        facts: list[Fact] | None = None,
    ) -> tuple[ApplicabilityDecision, list[str]]:
        """Validate one model draft without silently accepting invalid references."""
        if decision.control_id != control.control_id:
            return decision, ["control_id does not match the active Control"]
        known_fact_ids = {fact.fact_id for fact in facts or []}
        missing_facts = sorted(set(decision.technical_fact_refs) - known_fact_ids)
        if missing_facts:
            return decision, [
                "unknown technical_fact_refs: "
                f"{missing_facts}; allowed IDs are {sorted(known_fact_ids)}. "
                "Do not use file paths, symbols, search descriptions, or tool output text "
                "as technical_fact_refs."
            ]
        obligations_by_id = {
            obligation.obligation_id: obligation for obligation in obligations or []
        }
        allowed_source_refs = _allowed_source_refs(control, obligations_by_id)
        if decision.source_refs and not all(
            _ref_key(reference) in {_ref_key(item) for item in allowed_source_refs}
            for reference in decision.source_refs
        ):
            return decision, [
                "source_refs do not exactly match an allowed source reference. "
                "Copy one of these references without changing source_id, source_section, "
                f"path, or url: {[item.model_dump(mode='json') for item in allowed_source_refs]}"
            ]
        invalid_profile_refs = _invalid_profile_fact_refs(decision.profile_fact_refs, profile)
        if invalid_profile_refs:
            allowed_profile_refs = [
                {
                    "field_name": name,
                    "expected_value": _canonical_json(fact.value),
                    "source": fact.source,
                }
                for name, fact in sorted(profile.confirmed_facts.items())
            ]
            return decision, [
                "profile_fact_refs contain invalid field/value pairs: "
                f"{invalid_profile_refs}. Copy field_name and expected_value exactly "
                f"from allowed_profile_fact_refs: {allowed_profile_refs}"
            ]
        if decision.decision == "applicable":
            condition_fact_keys = set(_condition_fact_keys(control.applicability_condition))
            cited_profile_keys = {
                reference.field_name for reference in decision.profile_fact_refs
            }
            has_profile_provenance = bool(
                cited_profile_keys
                if control.applicability_condition.kind == "unknown"
                else cited_profile_keys & condition_fact_keys
            )
            has_technical_provenance = bool(decision.technical_fact_refs)
            if not has_profile_provenance and not has_technical_provenance:
                return decision, [
                    "applicable decision must cite a confirmed profile fact or a known "
                    "technical fact"
                ]
        validated = self.validate(
            profile,
            ControlSet(contract="control_set.v2", version="draft", controls=[control]),
            [decision],
            obligations=obligations,
            source_registry=source_registry,
        )[0]
        issues: list[str] = []
        if decision.decision != validated.decision:
            issues.append(
                "decision was downgraded because policy/profile provenance could not be verified"
            )
        if decision.decision == "not_applicable" and validated.decision != "not_applicable":
            issues.append("not_applicable requires valid policy and confirmed-fact references")
        return validated, issues

    def validate(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        decisions: list[ApplicabilityDecision],
        obligations: list[Obligation] | None = None,
        source_registry: SourceRegistry | None = None,
    ) -> list[ApplicabilityDecision]:
        by_id = {decision.control_id: decision for decision in decisions}
        expected_ids = {control.control_id for control in controls.controls}
        if set(by_id) != expected_ids or len(by_id) != len(decisions):
            raise ValueError("applicability decisions must cover every Control exactly once")
        normalized: list[ApplicabilityDecision] = []
        obligations_by_id = {
            obligation.obligation_id: obligation for obligation in obligations or []
        }
        for control in controls.controls:
            decision = by_id[control.control_id]
            allowed_source_refs = _allowed_source_refs(control, obligations_by_id)
            source_refs_valid = _source_refs_valid(
                decision.source_refs, allowed_source_refs, source_registry
            )
            profile_refs_valid = _profile_refs_valid(decision.profile_fact_refs, profile)
            surface_requirements = _validated_surface_requirements(
                control,
                decision.surface_requirements,
                profile,
                allowed_source_refs,
                source_registry,
            )
            resolved_required_surfaces = [
                item.surface
                for item in surface_requirements
                if item.decision == "required"
            ]
            if decision.decision == "not_applicable" and (
                not decision.source_refs
                or not decision.profile_fact_refs
                or not source_refs_valid
                or not profile_refs_valid
            ):
                normalized.append(
                    decision.model_copy(
                        update={
                            "decision": "unknown",
                            "reason": (
                                "not_applicable claim could not be verified against confirmed "
                                "profile facts and linked policy provenance; "
                                "retained conservatively"
                            ),
                            "unresolved_conditions": sorted(
                                set([*decision.unresolved_conditions, "unverified_not_applicable"])
                            ),
                            "confidence": "low",
                            "surface_requirements": surface_requirements,
                            "resolved_required_surfaces": resolved_required_surfaces,
                        }
                    )
                )
                continue
            if not source_refs_valid or not profile_refs_valid:
                normalized.append(
                    decision.model_copy(
                        update={
                            "decision": "unknown",
                            "reason": (
                                "applicability references do not match the linked policy source "
                                "or confirmed AppProfile facts"
                            ),
                            "unresolved_conditions": sorted(
                                set([*decision.unresolved_conditions, "unverified_references"])
                            ),
                            "confidence": "low",
                            "surface_requirements": surface_requirements,
                            "resolved_required_surfaces": resolved_required_surfaces,
                        }
                    )
                )
                continue
            normalized.append(
                decision.model_copy(
                    update={
                        "surface_requirements": surface_requirements,
                        "resolved_required_surfaces": resolved_required_surfaces,
                    }
                )
            )
        return normalized


class SemanticApplicabilityEvaluator:
    """One bounded structured call for applicability, not an Agent loop."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def evaluate(
        self,
        profile: ApplicabilityProfile,
        controls: ControlSet,
        source_registry: SourceRegistry | None = None,
        obligations: list[Obligation] | None = None,
    ) -> list[ApplicabilityDecision]:
        work_item = WorkItem(
            work_item_type="semantic_applicability",
            work_item_id="applicability.semantic",
            module_id="applicability",
            surface="other_external",
            control_ids=[control.control_id for control in controls.controls],
            allowed_roots=["."],
        )
        payload: dict[str, object] = {
            "confirmed_profile_facts": {
                name: fact.model_dump(mode="json") for name, fact in profile.confirmed_facts.items()
            },
            "controls": [
                {
                    "control_id": control.control_id,
                    "title": control.title,
                    "linked_policy_sources": [
                        reference.model_dump(mode="json") for reference in control.source_refs
                    ],
                    "obligations": _obligation_context(control, obligations or [], source_registry),
                    "candidate_evidence_surfaces": list(control.surface_candidates),
                    "candidate_surfaces": list(control.surface_candidates),
                }
                for control in controls.controls
            ],
        }
        candidate_surfaces = {
            surface
            for control in controls.controls
            for surface in control.surface_candidates
        }
        request = ModelRequest(
            work_item=work_item,
            attempt_id="applicability.semantic.v1",
            agent_id="applicability-evaluator",
            request_kind="applicability",
            token_budget=8_000,
            tools=[],
            response_schema=SemanticApplicabilityResponse.model_json_schema(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Decide applicability for every supplied control using policy context and "
                        "only supplied confirmed AppProfile facts. unknown is required when a "
                        "condition cannot be confirmed. not_applicable is allowed only when "
                        "source_refs and profile_fact_refs cite the specific supplied facts that "
                        "prove exclusion. profile_fact_refs.expected_value must be the canonical "
                        "JSON string for the supplied profile value. Do not invent sources or "
                        "profile values. Candidate evidence surfaces are a policy-level superset, "
                        "not a final review denominator. The validator resolves candidates against "
                        "the confirmed AppProfile delivery surfaces and any Control-defined "
                        "structured EvidenceRequirement condition. Do not invent a surface. The "
                        "validator, not the model, computes final "
                        "resolved_required_surfaces."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        _with_delivery_surface_facts(
                            payload, profile, candidate_surfaces
                        ),
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        response = self.provider.complete(request)
        if response.tool_calls or not response.content:
            raise ValueError(
                "semantic applicability response must be structured JSON without tools"
            )
        try:
            parsed = SemanticApplicabilityResponse.model_validate_json(response.content)
        except ValueError as exc:
            raise ValueError("semantic applicability response is missing valid decisions") from exc
        return parsed.decisions


def legacy_applicability_decision(
    control: Control, profile: ApplicabilityProfile
) -> ApplicabilityDecision:
    """Conservative compatibility fallback when no semantic provider is configured."""
    result = control_applicability(control, profile)
    refs = _legacy_profile_refs(control.applicability_expression, profile)
    if result is True:
        return ApplicabilityDecision(
            control_id=control.control_id,
            decision="applicable",
            reason="legacy applicability hint matches confirmed AppProfile facts",
            source_refs=control.source_refs,
            profile_fact_refs=refs,
            surface_requirements=_legacy_surface_requirements(control),
            confidence="medium",
        )
    return ApplicabilityDecision(
        control_id=control.control_id,
        decision="unknown",
        reason="semantic applicability provider is unavailable or the legacy hint is unresolved",
        source_refs=control.source_refs,
        profile_fact_refs=refs,
        unresolved_conditions=[control.applicability_expression],
        surface_requirements=_legacy_surface_requirements(control),
        confidence="low",
    )


def _build_applicability_set(
    profile: ApplicabilityProfile,
    controls: ControlSet,
    decisions: list[ApplicabilityDecision],
) -> ApplicabilitySet:
    """Build the final applicability ledger from validated decisions only."""
    expected = {control.control_id for control in controls.controls}
    actual = {decision.control_id for decision in decisions}
    if actual != expected or len(actual) != len(decisions):
        raise ValueError("Applicability Loop did not produce exactly one decision per Control")
    return ApplicabilitySet(
        contract="applicability_set.v2",
        profile_version=profile.version,
        control_version=controls.version,
        decisions=decisions,
        excluded_control_ids=sorted(
            decision.control_id
            for decision in decisions
            if decision.decision == "not_applicable"
        ),
        unknown_control_ids=sorted(
            decision.control_id for decision in decisions if decision.decision == "unknown"
        ),
    )


def _safe_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "fact"


def _safe_fact_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "fact"


def _condition_fact_keys(condition: ApplicabilityCondition) -> list[str]:
    if condition.kind == "atom":
        return [condition.fact] if condition.fact else []
    return sorted(
        {
            fact_key
            for child in condition.conditions
            for fact_key in _condition_fact_keys(child)
        }
    )


def _question_text(fact_key: str, control_title: str, reason: str) -> str:
    prompts = {
        "business_type": "该 App 的实际业务类型是什么（例如 personal_loan、EWA、marketplace）？",
        "self_lending": "该 App 是否由持牌主体直接向用户放贷？",
        "jurisdiction": "该 App 面向哪个国家或司法辖区运营？",
    }
    return prompts.get(
        fact_key,
        f"为判断控制项“{control_title}”，请补充事实：{reason}",
    )


def _issue_from_text(
    text: str, decisions: list[ApplicabilityDecision]
) -> ApplicabilityValidationIssue:
    control_id = decisions[0].control_id if len(decisions) == 1 else None
    return ApplicabilityValidationIssue(
        code="applicability_draft_validation",
        message=text,
        control_id=control_id,
    )


def _validated_surface_requirements(
    control: Control,
    provided: list[SurfaceRequirementDecision],
    profile: ApplicabilityProfile,
    allowed_source_refs: list[SourceRef],
    source_registry: SourceRegistry | None,
) -> list[SurfaceRequirementDecision]:
    # Controls provide a policy-level candidate superset. AppProfile declares
    # the surfaces actually in scope for this review; an absent configured
    # surface is not a missing implementation by itself. A Control condition
    # can further narrow an available surface, but cannot manufacture one.
    normalized: list[SurfaceRequirementDecision] = []
    for surface in control.surface_candidates:
        requirement = control.evidence_requirements.get(surface)
        condition = requirement.condition if requirement is not None else None
        if surface not in profile.evidence_surfaces:
            status: SurfaceRequirementStatus = "not_required"
            reason = "candidate surface is not configured in the confirmed AppProfile"
        elif condition is None:
            status = "required"
            reason = "candidate surface is configured in the confirmed AppProfile"
        else:
            evaluated = _evaluate_condition(condition, profile)
            if evaluated is True:
                status = "required"
                reason = "explicit evidence requirement condition evaluated true"
            elif evaluated is False:
                status = "not_required"
                reason = "explicit evidence requirement condition evaluated false"
            else:
                status = "unknown"
                reason = "explicit evidence requirement condition is unresolved"
        normalized.append(
            SurfaceRequirementDecision(
                surface=surface,
                decision=status,
                reason=reason,
                source_refs=(requirement.source_refs if requirement else list(control.source_refs)),
            )
        )
    return normalized


def _unknown_surface_requirement(surface: Surface, reason: str) -> SurfaceRequirementDecision:
    return SurfaceRequirementDecision(
        surface=surface,
        decision="unknown",
        reason=reason,
    )


def _legacy_surface_requirements(control: Control) -> list[SurfaceRequirementDecision]:
    return [
        SurfaceRequirementDecision(
            surface=surface,
            decision="required",
            reason="legacy control required_surfaces compatibility fallback",
            source_refs=control.source_refs,
        )
        for surface in control.surface_candidates
    ]


def _obligation_context(
    control: Control,
    obligations: list[Obligation],
    source_registry: SourceRegistry | None,
) -> list[dict[str, object]]:
    obligations_by_id = {obligation.obligation_id: obligation for obligation in obligations}
    sources_by_id = {
        source.source_id: source for source in (source_registry.sources if source_registry else [])
    }
    context: list[dict[str, object]] = []
    for obligation_id in control.obligation_ids:
        obligation = obligations_by_id.get(obligation_id)
        if obligation is None:
            continue
        source = sources_by_id.get(obligation.source_id)
        section = next(
            (item for item in source.sections if item.section_id == obligation.source_section),
            None,
        ) if source else None
        context.append(
            {
                "obligation_id": obligation.obligation_id,
                "statement": obligation.statement,
                "concepts": obligation.concepts,
                "applicability_condition": obligation.applicability_condition.model_dump(
                    mode="json"
                ),
                "source_id": obligation.source_id,
                "source_section": obligation.source_section,
                "section": (
                    {
                        "section_id": section.section_id,
                        "title": section.title,
                        "text": section.text[:16000],
                        "location": section.location,
                        "page": section.page,
                        "page_end": section.page_end,
                    }
                    if section
                    else None
                ),
            }
        )
    return context


def _with_delivery_surface_facts(
    payload: dict[str, object],
    profile: ApplicabilityProfile,
    candidate_surfaces: set[Surface],
) -> dict[str, object]:
    confirmed = profile.confirmed_facts.get("evidence_surfaces")
    surface_facts = [
        {
            "surface": surface,
            "present": surface in profile.evidence_surfaces,
            "root": profile.roots.get(surface),
            "confirmation_source": confirmed.source if confirmed else "unresolved",
        }
        for surface in sorted(
            set(profile.evidence_surfaces) | set(profile.roots) | candidate_surfaces
        )
    ]
    return {**payload, "delivery_surface_facts": surface_facts}


def control_applicability(control: Control, profile: ApplicabilityProfile) -> bool | None:
    """Evaluate only the legacy no-provider fallback over structured data."""
    return _evaluate_condition(control.applicability_condition, profile)


def _source_refs_valid(
    provided: list[SourceRef],
    allowed: list[SourceRef],
    source_registry: SourceRegistry | None = None,
) -> bool:
    allowed_refs = {_ref_key(reference) for reference in allowed}
    if not all(_ref_key(reference) in allowed_refs for reference in provided):
        return False
    if source_registry is None:
        return True
    sources = {source.source_id: source for source in source_registry.sources}
    for reference in provided:
        if reference.source_id is None:
            continue
        source = sources.get(reference.source_id)
        if source is None:
            return False
        if reference.source_section and not any(
            section.section_id == reference.source_section for section in source.sections
        ):
            return False
    return True


def _allowed_source_refs(
    control: Control, obligations_by_id: dict[str, Obligation]
) -> list[SourceRef]:
    refs = [*control.source_refs]
    for obligation_id in control.obligation_ids:
        obligation = obligations_by_id.get(obligation_id)
        if obligation is not None:
            refs.extend(obligation.source_refs)
    return list({_ref_key(reference): reference for reference in refs}.values())


def _profile_refs_valid(
    provided: list[ProfileFactRef],
    profile: ApplicabilityProfile,
    trusted_sources: set[str] | None = None,
) -> bool:
    allowed_sources = trusted_sources or {"declared", "human_confirmed", "deterministic"}
    for reference in provided:
        fact = profile.confirmed_facts.get(reference.field_name)
        if (
            fact is None
            or fact.source not in allowed_sources
            or _canonical_json(fact.value) != reference.expected_value
        ):
            return False
    return True


def _invalid_profile_fact_refs(
    provided: list[ProfileFactRef],
    profile: ApplicabilityProfile,
    trusted_sources: set[str] | None = None,
) -> list[dict[str, str]]:
    allowed_sources = trusted_sources or {"declared", "human_confirmed", "deterministic"}
    invalid: list[dict[str, str]] = []
    for reference in provided:
        fact = profile.confirmed_facts.get(reference.field_name)
        if (
            fact is None
            or fact.source not in allowed_sources
            or _canonical_json(fact.value) != reference.expected_value
        ):
            invalid.append(reference.model_dump(mode="json"))
    return invalid


def _ref_key(reference: SourceRef) -> str:
    return json.dumps(reference.model_dump(mode="json", exclude_none=True), sort_keys=True)


def _legacy_profile_refs(expression: str, profile: ApplicabilityProfile) -> list[ProfileFactRef]:
    names: set[str] = set()
    for clause in re.split(r"\s+(?:and|&&)\s+", expression, flags=re.IGNORECASE):
        match = _INCLUDES_RE.match(clause) or _EQUALS_RE.match(clause) or _IN_RE.match(clause)
        if match:
            names.add(match.group("field"))
    refs: list[ProfileFactRef] = []
    for name in sorted(names):
        fact = profile.confirmed_facts.get(name)
        if fact is not None:
            refs.append(ProfileFactRef(field_name=name, expected_value=_canonical_json(fact.value)))
    return refs


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evaluate_clause(clause: str, profile: ApplicabilityProfile) -> bool | None:
    match = _INCLUDES_RE.match(clause) or _EQUALS_RE.match(clause) or _IN_RE.match(clause)
    if not match:
        return None
    field = match.group("field")
    raw_value = match.group("value").strip().strip("\"'")
    if field == "business_type":
        return raw_value in profile.business_type
    if field == "evidence_surfaces":
        return raw_value in profile.evidence_surfaces
    if field == "self_lending":
        if profile.self_lending == "unknown":
            return None
        return profile.self_lending is (raw_value.lower() == "true")
    if field == "jurisdiction":
        return profile.jurisdiction == raw_value
    return None


def _evaluate_condition(
    condition: ApplicabilityCondition, profile: ApplicabilityProfile
) -> bool | None:
    if condition.kind == "unknown":
        return None
    if condition.kind == "all_of":
        values = [_evaluate_condition(child, profile) for child in condition.conditions]
        return (
            None
            if any(value is None for value in values)
            else all(value is True for value in values)
        )
    if condition.kind == "any_of":
        values = [_evaluate_condition(child, profile) for child in condition.conditions]
        if any(value is True for value in values):
            return True
        return None if any(value is None for value in values) else False
    observed: object
    profile_fact = profile.confirmed_facts.get(condition.fact or "")
    if profile_fact is not None:
        if profile_fact.source in {"unresolved", "inferred"}:
            return None
        observed = profile_fact.value
    elif condition.fact == "business_type":
        observed = profile.business_type
    elif condition.fact == "evidence_surfaces":
        observed = profile.evidence_surfaces
    elif condition.fact == "self_lending":
        observed = profile.self_lending
    elif condition.fact == "jurisdiction":
        observed = profile.jurisdiction
    else:
        return None
    if observed == "unknown":
        return None
    if condition.operator == "includes":
        if not isinstance(observed, (list, tuple, set)):
            return None
        return bool(condition.value in observed)
    if condition.operator == "equals":
        return bool(observed == condition.value)
    return None
