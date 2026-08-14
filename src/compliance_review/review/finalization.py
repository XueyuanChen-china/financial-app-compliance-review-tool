from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Literal, Mapping, Sequence

from compliance_review.collectors.base import CollectorResult
from compliance_review.domain.models import (
    ControlSet,
    ControlStatus,
    ControlSurfaceResult,
    CoverageGateResult,
    CoverageManifestRow,
    CoverageSet,
    EvidenceAnchor,
    EvidenceStatus,
    EvidenceStrength,
    ExecutionStatus,
    Fact,
    ResolvedControlResult,
    Surface,
    WorkItem,
)
from compliance_review.repository import RepositorySandbox
from compliance_review.review.evidence import (
    EVIDENCE_STRENGTH_RANK,
    file_content_revision,
    normalize_snippet,
    relocate_anchor,
    strongest_evidence_strength,
)
from compliance_review.review.models import (
    ModelRequest,
    ResultValidationResult,
    ReviewRunSummary,
    SuspiciousReviewSet,
    ValidatedReviewRow,
    ValidationIssue,
    VerifierResult,
    WorkerExecution,
)
from compliance_review.review.provider import ModelProvider

AUTOMATABLE_SURFACES: frozenset[Surface] = frozenset(
    {"frontend_h5", "android_native", "backend_code", "backend_api_doc"}
)


def is_automatable_surface(surface: Surface) -> bool:
    return surface in AUTOMATABLE_SURFACES


class ResultValidator:
    """Validate reviewer rows and their anchors against the current repositories."""

    def validate(
        self,
        summary: ReviewRunSummary,
        coverage: CoverageSet,
        controls: ControlSet,
        sandboxes: Mapping[str, RepositorySandbox],
        work_items: Sequence[WorkItem] = (),
        collector_results: Mapping[str, CollectorResult] | None = None,
    ) -> ResultValidationResult:
        control_by_id = {item.control_id: item for item in controls.controls}
        work_items_by_id = {item.work_item_id: item for item in work_items}
        facts_by_id, duplicate_fact_ids = _build_fact_registry(collector_results or {})
        reviewer_rows: dict[
            tuple[str, Surface], list[tuple[WorkerExecution, ControlSurfaceResult]]
        ] = defaultdict(list)
        anchors_by_execution: dict[str, dict[str, EvidenceAnchor]] = {}
        duplicate_anchor_ids: set[tuple[str, str]] = set()
        execution_issues: dict[str, list[ValidationIssue]] = defaultdict(list)
        for execution in summary.executions:
            result = execution.result
            if result is None:
                continue
            identity_mismatch = (
                result.work_item_id != execution.work_item_id
                or result.attempt_id != execution.attempt_id
                or result.agent_id != execution.agent_id
                or result.execution_status != execution.execution_status
                or execution.execution_status != "completed"
            )
            if identity_mismatch:
                execution_issues[execution.attempt_id].append(
                    _error(
                        "execution_result_mismatch",
                        "Worker execution and ReviewResult identity or status disagree.",
                    )
                )
            assigned = work_items_by_id.get(execution.work_item_id)
            anchor_map: dict[str, EvidenceAnchor] = {}
            for anchor in result.anchors:
                if anchor.anchor_id in anchor_map:
                    duplicate_anchor_ids.add((execution.attempt_id, anchor.anchor_id))
                anchor_map[anchor.anchor_id] = anchor
            anchors_by_execution[execution.attempt_id] = anchor_map
            seen_rows: set[tuple[str, Surface]] = set()
            for row in result.rows:
                key = (row.control_id, row.surface)
                if key in seen_rows:
                    execution_issues[execution.attempt_id].append(
                        _error(
                            "duplicate_reviewer_row",
                            "One ReviewResult repeats a Control and surface row.",
                        )
                    )
                seen_rows.add(key)
                if assigned is not None and (
                    row.surface != assigned.surface or row.control_id not in assigned.control_ids
                ):
                    execution_issues[execution.attempt_id].append(
                        _error(
                            "out_of_scope_reviewer_row",
                            "Reviewer row is outside the assigned Work Item.",
                        )
                    )
                reviewer_rows[key].append((execution, row))

        validated: list[ValidatedReviewRow] = []
        errors: list[str] = []
        for unit in coverage.units:
            row_id = f"{unit.control_id}:{unit.surface}"
            issues: list[ValidationIssue] = []
            claims = reviewer_rows.get((unit.control_id, unit.surface), [])
            owned_claims = [
                pair
                for pair in claims
                if unit.work_item_id is None or pair[0].work_item_id == unit.work_item_id
            ]
            selected_execution = owned_claims[0][0] if len(owned_claims) == 1 else None
            selected_row = owned_claims[0][1] if len(owned_claims) == 1 else None
            control = control_by_id.get(unit.control_id)
            if control is None:
                issues.append(_error("unknown_control", "Coverage references an unknown control."))
            if unit.coverage_status == "not_applicable":
                selected_execution = None
                selected_row = None
            elif unit.coverage_status == "missing_surface":
                issues.append(
                    _error(
                        "missing_required_surface",
                        f"Required surface is unavailable: {unit.surface}.",
                    )
                )
            elif unit.coverage_status == "unknown_applicability":
                issues.append(
                    _error("unknown_applicability", "Control applicability is not resolved.")
                )
            elif len(owned_claims) > 1:
                issues.append(
                    _error(
                        "duplicate_reviewer_row",
                        "Multiple reviewer rows claim the same Control and surface.",
                    )
                )
            elif selected_row is None:
                if claims:
                    issues.append(
                        _error(
                            "work_item_claim_mismatch",
                            "Reviewer row was produced by a different Work Item.",
                        )
                    )
                else:
                    issues.append(
                        _error("missing_reviewer_row", "No reviewer row covers this unit.")
                    )
            else:
                assert selected_execution is not None
                issues.extend(execution_issues.get(selected_execution.attempt_id, []))
                anchor_map = anchors_by_execution.get(selected_execution.attempt_id, {})
                if selected_row.recommended_control_status in {"waived", "not_applicable"}:
                    issues.append(
                        _error(
                            "reviewer_cannot_set_exemption",
                            "Reviewer cannot create waiver or applicability decisions.",
                        )
                    )
                if (
                    selected_row.evidence_status == "manual_required"
                    and selected_row.recommended_control_status != "indeterminate"
                ):
                    issues.append(
                        _error(
                            "manual_evidence_status_mismatch",
                            "Manual-required evidence must recommend indeterminate.",
                        )
                    )
                if (
                    selected_row.evidence_status != "complete"
                    and selected_row.recommended_control_status == "pass"
                ):
                    issues.append(
                        _error("pass_without_complete_evidence", "PASS requires complete evidence.")
                    )
                observed = selected_row.observed_evidence_strength
                if selected_row.evidence_status == "complete":
                    if observed is None:
                        issues.append(
                            _error("missing_evidence_strength", "Observed strength is absent.")
                        )
                    elif (
                        EVIDENCE_STRENGTH_RANK[observed]
                        < EVIDENCE_STRENGTH_RANK[unit.required_evidence_strength]
                    ):
                        issues.append(
                            _error(
                                "insufficient_evidence_strength",
                                f"{observed} is below required {unit.required_evidence_strength}.",
                            )
                        )
                if selected_row.unsupported_inferences:
                    issue_factory = (
                        _error
                        if selected_row.recommended_control_status == "pass"
                        else _flag
                    )
                    issues.append(
                        issue_factory(
                            "unsupported_inference",
                            "Reviewer reported unsupported inference.",
                        )
                    )
                if selected_row.confidence == "low":
                    issue_factory = (
                        _error
                        if selected_row.recommended_control_status == "pass"
                        else _flag
                    )
                    issues.append(issue_factory("low_confidence", "Reviewer confidence is low."))
                if selected_row.recommended_control_status == "pass" and control is not None:
                    if control.severity in {"critical", "high"}:
                        issues.append(
                            _flag(
                                "high_severity_pass",
                                "High-severity PASS should receive human attention.",
                            )
                        )
                    if observed == unit.required_evidence_strength:
                        issues.append(
                            _flag(
                                "minimum_threshold_pass",
                                "PASS rests exactly on the minimum evidence threshold.",
                            )
                        )
                if (
                    selected_row.evidence_status == "complete"
                    and selected_row.recommended_control_status in {"pass", "fail"}
                    and not selected_row.anchor_ids
                ):
                    issues.append(
                        _error(
                            "terminal_result_without_anchor",
                            "A complete PASS or FAIL result requires an evidence anchor.",
                        )
                    )
                cited_strengths: list[EvidenceStrength] = []
                for anchor_id in selected_row.anchor_ids:
                    if (selected_execution.attempt_id, anchor_id) in duplicate_anchor_ids:
                        issues.append(
                            _error(
                                "duplicate_anchor_id",
                                f"Anchor ID is duplicated in one execution: {anchor_id}.",
                            )
                        )
                        continue
                    selected_anchor = anchor_map.get(anchor_id)
                    if selected_anchor is None:
                        issues.append(
                            _error(
                                "unknown_anchor",
                                f"Anchor does not exist: {anchor_id}.",
                            )
                        )
                        continue
                    cited_strengths.append(selected_anchor.evidence_strength)
                    issues.extend(
                        _validate_anchor(
                            selected_anchor,
                            unit.control_id,
                            unit.surface,
                            sandboxes,
                            selected_execution.work_item_id,
                        )
                    )
                cited_fact_ids = {
                    fact_id
                    for anchor_id in selected_row.anchor_ids
                    for anchor in [anchor_map.get(anchor_id)]
                    if anchor is not None
                    for fact_id in anchor.fact_ids
                }
                declared_fact_ids = set(selected_row.fact_ids)
                if declared_fact_ids != cited_fact_ids:
                    issues.append(
                        _error(
                            "row_fact_provenance_mismatch",
                            "Row fact IDs must exactly match facts carried by cited anchors.",
                        )
                    )
                assigned = work_items_by_id.get(selected_execution.work_item_id)
                allowed_fact_ids = set(assigned.collector_fact_refs) if assigned else set()
                for fact_id in sorted(cited_fact_ids):
                    issues.extend(
                        _validate_fact_reference(
                            fact_id,
                            unit.surface,
                            allowed_fact_ids,
                            facts_by_id,
                            duplicate_fact_ids,
                            [
                                anchor
                                for anchor_id in selected_row.anchor_ids
                                for anchor in [anchor_map.get(anchor_id)]
                                if anchor is not None and fact_id in anchor.fact_ids
                            ],
                        )
                    )
                if observed is not None and cited_strengths:
                    strongest_anchor = strongest_evidence_strength(cited_strengths)
                    if (
                        strongest_anchor is not None
                        and EVIDENCE_STRENGTH_RANK[observed]
                        > EVIDENCE_STRENGTH_RANK[strongest_anchor]
                    ):
                        issues.append(
                            _error(
                                "anchor_strength_mismatch",
                                "Reviewer strength exceeds the strongest cited anchor.",
                            )
                        )

            invalid = any(issue.severity == "error" for issue in issues)
            flags = [issue.code for issue in issues if _is_flag(issue)]
            validated.append(
                ValidatedReviewRow(
                    row_id=row_id,
                    control_id=unit.control_id,
                    surface=unit.surface,
                    work_item_id=(
                        selected_execution.work_item_id
                        if selected_execution is not None
                        else unit.work_item_id
                    ),
                    attempt_id=(
                        selected_execution.attempt_id if selected_execution is not None else None
                    ),
                    row=selected_row,
                    valid=not invalid,
                    flags=flags,
                    # Deprecated compatibility alias; authoritative code reads flags.
                    suspicious=bool(flags),
                    issues=issues,
                )
            )
            errors.extend(f"{row_id}:{issue.code}" for issue in issues if issue.severity == "error")

        _mark_cross_surface_conflicts(validated)
        for validated_row in validated:
            validated_row.flags = sorted(
                {
                    *validated_row.flags,
                    *[
                        issue.code
                        for issue in validated_row.issues
                        if _is_flag(issue)
                    ],
                }
            )
            validated_row.suspicious = bool(validated_row.flags)
        flag_map = {item.row_id: item.flags for item in validated if item.flags}
        suspicious_ids = sorted(flag_map)
        return ResultValidationResult(
            valid=not errors,
            rows=validated,
            flags=flag_map,
            suspicious_row_ids=suspicious_ids,
            errors=errors,
        )


class SuspiciousRouter:
    """Deprecated compatibility adapter; flags are authoritative now."""

    def route(self, validation: ResultValidationResult) -> SuspiciousReviewSet:
        selected = [row for row in validation.rows if row.suspicious]
        return SuspiciousReviewSet(
            row_ids=[row.row_id for row in selected],
            reasons={row.row_id: [issue.code for issue in row.issues] for row in selected},
        )


class TargetedVerifier:
    """Deprecated compatibility verifier; never used by authoritative runs."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def verify(
        self,
        suspicious: SuspiciousReviewSet,
        validation: ResultValidationResult,
        controls: ControlSet,
    ) -> VerifierResult:
        if not suspicious.row_ids:
            return VerifierResult(status="not_required")
        first = next(row for row in validation.rows if row.row_id in suspicious.row_ids)
        work_item = WorkItem(
            work_item_id="verification.targeted_qa",
            module_id="verification",
            surface=first.surface,
            control_ids=sorted(
                {row.control_id for row in validation.rows if row.row_id in suspicious.row_ids}
            ),
        )
        payload = {
            "suspicious": suspicious.model_dump(),
            "rows": [
                row.model_dump(mode="json")
                for row in validation.rows
                if row.row_id in suspicious.row_ids
            ],
            "controls": [
                control.model_dump(mode="json")
                for control in controls.controls
                if control.control_id in work_item.control_ids
            ],
        }
        try:
            response = self.provider.complete(
                ModelRequest(
                    work_item=work_item,
                    attempt_id="verification-1",
                    agent_id="verifier-001",
                    request_kind="verification",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Perform targeted QA only on supplied suspicious rows. "
                                "Return one verifier_result.v1 JSON object. Do not invent evidence."
                            ),
                        },
                        {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                    ],
                    tools=[],
                )
            )
            if response.tool_calls or not response.content:
                raise ValueError("verifier must return structured content without tools")
            result = VerifierResult.model_validate_json(response.content)
            expected = set(suspicious.row_ids)
            decision_ids = [decision.row_id for decision in result.decisions]
            actual = set(decision_ids)
            errors = list(result.errors)
            if result.status != "completed":
                errors.append("verifier must report completed for suspicious rows")
            if actual != expected or len(decision_ids) != len(actual):
                errors.append(
                    "verifier row coverage mismatch: "
                    f"expected={sorted(expected)} actual={sorted(actual)}"
                )
            rows_by_id = {row.row_id: row for row in validation.rows}
            for decision in result.decisions:
                row = rows_by_id.get(decision.row_id)
                if row is None or row.row is None:
                    errors.append(
                        f"verifier decision references an unavailable row: {decision.row_id}"
                    )
                    continue
                if decision.decision == "confirm":
                    if (
                        decision.recommended_status is not None
                        and decision.recommended_status != row.row.recommended_control_status
                    ):
                        errors.append(
                            f"verifier confirm contradicts reviewer status: {decision.row_id}"
                        )
                    if set(decision.anchor_ids) != set(row.row.anchor_ids):
                        errors.append(f"verifier confirm anchor set mismatch: {decision.row_id}")
                elif decision.decision == "correction" and decision.recommended_status in {
                    "pass",
                    "waived",
                    "not_applicable",
                }:
                    errors.append(
                        f"verifier correction cannot authorize {decision.recommended_status}: "
                        f"{decision.row_id}"
                    )
            if errors:
                return result.model_copy(update={"status": "partial", "errors": errors})
            return result
        except Exception as exc:
            return VerifierResult(status="failed", errors=[str(exc)])


class ComplianceResolver:
    def resolve(
        self,
        controls: ControlSet,
        coverage: CoverageSet,
        validation: ResultValidationResult,
        verifier_result: VerifierResult | None = None,
    ) -> list[ResolvedControlResult]:
        # verifier_result is retained for API compatibility only. It is never
        # consulted by the authoritative resolver.
        del verifier_result
        rows_by_control: dict[str, list[ValidatedReviewRow]] = defaultdict(list)
        units_by_control = defaultdict(list)
        for row in validation.rows:
            rows_by_control[row.control_id].append(row)
        for unit in coverage.units:
            units_by_control[unit.control_id].append(unit)
        resolved: list[ResolvedControlResult] = []
        for control in controls.controls:
            units = units_by_control[control.control_id]
            rows = rows_by_control.get(control.control_id, [])
            reasons: list[str] = []
            if control.control_id in coverage.excluded_control_ids:
                status: ControlStatus = "not_applicable"
                reasons.append("Control applicability evaluated false.")
            elif any(
                row.valid
                and row.row is not None
                and row.row.evidence_status == "complete"
                and row.row.recommended_control_status == "fail"
                for row in rows
            ):
                status = "fail"
                reasons.append("Validated reviewer evidence explicitly supports failure.")
            elif not units or any(
                unit.coverage_status not in {"planned", "not_applicable"} for unit in units
            ):
                status = "indeterminate"
                reasons.append(
                    "One or more required evidence surfaces are unavailable or unresolved."
                )
            elif len(rows) != len(units) or any(
                not row.valid or row.row is None or row.row.evidence_status != "complete"
                for row in rows
            ):
                status = "indeterminate"
                reasons.append("Validated evidence is incomplete or invalid.")
            elif all(
                row.row is not None and row.row.recommended_control_status == "pass" for row in rows
            ):
                status = "pass"
                reasons.append("All required surfaces have validated complete evidence.")
            else:
                status = "indeterminate"
                reasons.append(
                    "Reviewer recommendations do not support a deterministic terminal result."
                )
            resolved.append(
                ResolvedControlResult(
                    control_id=control.control_id,
                    status=status,
                    severity=control.severity,
                    coverage_unit_ids=[unit.coverage_unit_id for unit in units],
                    reasons=reasons,
                )
            )
        return resolved


class CoverageGate:
    def evaluate(
        self,
        controls: ControlSet,
        coverage: CoverageSet,
        validation: ResultValidationResult,
        resolved: list[ResolvedControlResult],
        *,
        mode: Literal["standard", "full", "diff"] = "standard",
        previous_manual_ids: Sequence[str] = (),
        automated_evidence_regression_ids: Sequence[str] = (),
    ) -> CoverageGateResult:
        control_by_id = {item.control_id: item for item in controls.controls}
        result_by_id = {item.control_id: item for item in resolved}
        validated_by_id = {item.row_id: item for item in validation.rows}
        rows: list[CoverageManifestRow] = []
        blocking: list[str] = []
        warnings: list[str] = []
        for unit in coverage.units:
            row_id = f"{unit.control_id}:{unit.surface}"
            validated = validated_by_id.get(row_id)
            resolution = result_by_id[unit.control_id]
            reviewed_row = validated.row if validated is not None else None
            origin: Literal[
                "reviewed",
                "reused",
                "manual_required",
                "blocked",
                "not_applicable",
                "waived",
            ]
            execution_status: ExecutionStatus
            evidence_status: EvidenceStatus
            if unit.coverage_status == "not_applicable":
                origin = "not_applicable"
                execution_status = "completed"
                evidence_status = "complete"
            elif (
                validated is not None
                and validated.valid
                and reviewed_row is not None
                and reviewed_row.evidence_status == "manual_required"
            ):
                origin = "manual_required"
                execution_status = "completed"
                evidence_status = "manual_required"
            elif not is_automatable_surface(unit.surface):
                # External/manual surfaces are valid ledger entries even when
                # no automated Reviewer row exists. They remain manual work.
                origin = "manual_required"
                execution_status = "completed"
                evidence_status = "manual_required"
            elif (
                validated is not None
                and validated.valid
                and reviewed_row is not None
                and reviewed_row.evidence_status == "complete"
            ):
                origin = "reused" if validated.result_origin == "reused" else "reviewed"
                execution_status = "completed"
                evidence_status = "complete"
            else:
                origin = "blocked"
                execution_status = "failed"
                evidence_status = (
                    reviewed_row.evidence_status if reviewed_row is not None else "missing"
                )
            rows.append(
                CoverageManifestRow(
                    coverage_unit_id=unit.coverage_unit_id,
                    control_id=unit.control_id,
                    surface=unit.surface,
                    work_item_id=validated.work_item_id if validated else unit.work_item_id,
                    attempt_id=validated.attempt_id if validated else None,
                    execution_status=execution_status,
                    evidence_status=evidence_status,
                    result_origin=origin,
                    previous_run_id=validated.previous_run_id if validated else None,
                    resolution_status=resolution.status,
                )
            )
        for result in resolved:
            control = control_by_id[result.control_id]
            message = f"{result.control_id}:{result.status}"
            if result.status == "fail":
                blocking.append(message)
            elif result.status == "indeterminate":
                units = [unit for unit in coverage.units if unit.control_id == result.control_id]
                rows_by_unit = {row.coverage_unit_id: row for row in rows}
                has_automated_gap = any(
                    is_automatable_surface(unit.surface)
                    and (
                        rows_by_unit[unit.coverage_unit_id].result_origin == "blocked"
                        or rows_by_unit[unit.coverage_unit_id].evidence_status != "complete"
                    )
                    for unit in units
                    if unit.coverage_unit_id in rows_by_unit
                )
                if mode in {"full", "diff"} and has_automated_gap:
                    blocking.append(message)
                elif mode == "standard":
                    (blocking if control.missing_evidence_policy == "block" else warnings).append(
                        message
                    )
        coverage_ids = [row.coverage_unit_id for row in rows]
        expected_ids = [unit.coverage_unit_id for unit in coverage.units]
        complete = (
            len(coverage_ids) == len(set(coverage_ids))
            and set(coverage_ids) == set(expected_ids)
            and all(
                row.result_origin
                in {"reviewed", "reused", "manual_required", "not_applicable", "waived"}
                for row in rows
            )
        )
        current_manual_ids = sorted(
            row.coverage_unit_id for row in rows if row.result_origin == "manual_required"
        )
        previous_manual_set = set(previous_manual_ids)
        current_manual_set = set(current_manual_ids)
        if mode == "diff":
            manual_new_ids = sorted(current_manual_set - previous_manual_set)
            manual_existing_ids = sorted(current_manual_set & previous_manual_set)
            manual_resolved_ids = sorted(previous_manual_set - current_manual_set)
            warnings.extend(f"new_manual_required:{item}" for item in manual_new_ids)
        else:
            manual_new_ids = []
            manual_existing_ids = current_manual_ids if mode == "full" else []
            manual_resolved_ids = []
        automated_ids = sorted(set(automated_evidence_regression_ids))
        blocking.extend(f"automated_evidence_regression:{item}" for item in automated_ids)
        if not complete:
            blocking.append("coverage_incomplete")
        ci_status: Literal["pass", "warn", "block"] = (
            "block" if blocking else "warn" if warnings else "pass"
        )
        return CoverageGateResult(
            complete=complete,
            ci_status=ci_status,
            rows=rows,
            blocking_reasons=blocking,
            warning_reasons=warnings,
            validation_flags=validation.flags,
            manual_review_new_ids=manual_new_ids,
            manual_review_existing_ids=manual_existing_ids,
            manual_review_resolved_ids=manual_resolved_ids,
            automated_evidence_regression_ids=automated_ids,
        )


def _build_fact_registry(
    collector_results: Mapping[str, CollectorResult],
) -> tuple[dict[str, tuple[CollectorResult, Fact]], set[str]]:
    facts: dict[str, tuple[CollectorResult, Fact]] = {}
    duplicates: set[str] = set()
    for collector in collector_results.values():
        for fact in collector.facts:
            if fact.fact_id in facts:
                duplicates.add(fact.fact_id)
            facts[fact.fact_id] = (collector, fact)
    return facts, duplicates


def _validate_fact_reference(
    fact_id: str,
    surface: Surface,
    allowed_fact_ids: set[str],
    facts_by_id: Mapping[str, tuple[CollectorResult, Fact]],
    duplicate_fact_ids: set[str],
    anchors: Sequence[EvidenceAnchor],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if fact_id in duplicate_fact_ids:
        return [_error("duplicate_fact_id", f"Fact ID is not unique: {fact_id}.")]
    entry = facts_by_id.get(fact_id)
    if entry is None:
        return [_error("unknown_fact", f"Fact does not exist: {fact_id}.")]
    collector, fact = entry
    if fact_id not in allowed_fact_ids:
        issues.append(_error("fact_out_of_scope", f"Fact is not assigned: {fact_id}."))
    if collector.source_surface != surface or fact.source_surface != surface:
        issues.append(_error("fact_surface_mismatch", "Fact belongs to another surface."))
    if collector.parser_status != "ok" or fact.parser_status != "ok":
        issues.append(
            _error(
                "fact_not_authoritative",
                "Degraded Collector facts cannot support a terminal result.",
            )
        )
    elif collector.coverage_status == "unknown" or fact.coverage_status == "unknown":
        issues.append(
            _error(
                "fact_not_authoritative",
                "Collector facts with unknown coverage cannot support a terminal result.",
            )
        )
    source_paths = {Path(ref.path).as_posix() for ref in fact.source_refs if ref.path is not None}
    for anchor in anchors:
        if anchor.path is not None and source_paths:
            anchor_path = Path(anchor.path).as_posix()
            if not any(
                source_path == anchor_path or source_path.endswith(f"/{anchor_path}")
                for source_path in source_paths
            ):
                issues.append(
                    _error(
                        "fact_anchor_path_mismatch",
                        "Fact source path does not match its cited anchor.",
                    )
                )
        if (
            EVIDENCE_STRENGTH_RANK[anchor.evidence_strength]
            > EVIDENCE_STRENGTH_RANK[fact.evidence_strength]
        ):
            issues.append(
                _error(
                    "fact_strength_mismatch",
                    "Anchor strength exceeds the cited Collector fact.",
                )
            )
    return issues


def _validate_anchor(
    anchor: EvidenceAnchor,
    control_id: str,
    surface: Surface,
    sandboxes: Mapping[str, RepositorySandbox],
    work_item_id: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if control_id not in anchor.control_ids:
        issues.append(_error("anchor_control_mismatch", "Anchor does not cite this control."))
    if anchor.source_surface != surface:
        issues.append(_error("anchor_surface_mismatch", "Anchor belongs to another surface."))
    if anchor.path is None:
        issues.append(_error("anchor_without_path", "Anchor has no exact file path."))
        return issues
    sandbox = sandboxes.get(work_item_id) or sandboxes.get(surface)
    if sandbox is None:
        issues.append(_error("anchor_surface_unavailable", "No sandbox exists for anchor surface."))
        return issues
    try:
        text = sandbox.read_text(anchor.path)
    except (OSError, ValueError) as exc:
        issues.append(_error("anchor_path_invalid", str(exc)))
        return issues
    if not anchor.exact_snippet:
        issues.append(_error("anchor_not_exact", "Anchor has no exact snippet."))
        return issues
    if anchor.normalized_snippet_hash is None:
        issues.append(_error("anchor_hash_missing", "Anchor has no snippet hash."))
    if anchor.file_revision is None:
        issues.append(_error("anchor_revision_missing", "Anchor has no file revision."))
    normalized = normalize_snippet(anchor.exact_snippet)
    expected_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if anchor.normalized_snippet_hash != expected_hash:
        issues.append(_error("anchor_hash_mismatch", "Anchor snippet hash is invalid."))
    current_revision = file_content_revision(sandbox.read_bytes(anchor.path))
    relocation = relocate_anchor(anchor, text, current_revision, anchor.file_revision)
    if relocation.status == "missing":
        issues.append(
            _error(
                "anchor_snippet_not_found",
                "Anchor snippet is absent from current file.",
            )
        )
    elif relocation.status == "ambiguous":
        issues.append(
            _error(
                "anchor_ambiguous",
                "Anchor snippet has multiple possible current locations.",
            )
        )
    elif relocation.status == "relocated":
        issues.append(
            _flag(
                "anchor_relocated",
                "Anchor uniquely relocated to lines "
                f"{relocation.new_start_line}-{relocation.new_end_line}.",
            )
        )
    if (
        anchor.file_revision is not None
        and anchor.file_revision != current_revision
        and relocation.status in {"missing", "ambiguous"}
    ):
        issues.append(
            _error(
                "anchor_revision_mismatch",
                "Anchor was captured from another repository revision.",
            )
        )
    return issues


def _mark_cross_surface_conflicts(rows: list[ValidatedReviewRow]) -> None:
    grouped: dict[str, list[ValidatedReviewRow]] = defaultdict(list)
    for row in rows:
        grouped[row.control_id].append(row)
    for control_rows in grouped.values():
        statuses = {
            row.row.recommended_control_status
            for row in control_rows
            if row.row is not None and row.valid
        }
        if "pass" in statuses and "fail" in statuses:
            for row in control_rows:
                row.suspicious = True
                row.flags = sorted({*row.flags, "cross_surface_conflict"})
                row.issues.append(
                    _flag(
                        "cross_surface_conflict",
                        "Surfaces recommend conflicting outcomes.",
                    )
                )


def _error(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity="error", message=message)


def _flag(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity="flag", message=message)


def _is_flag(issue: ValidationIssue) -> bool:
    return issue.severity in {"flag", "suspicious"}
