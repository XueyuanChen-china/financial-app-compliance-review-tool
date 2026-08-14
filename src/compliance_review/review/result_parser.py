from __future__ import annotations

import json
from typing import Any, cast

from compliance_review.domain.models import (
    ControlStatus,
    ControlSurfaceResult,
    EvidenceStatus,
    ReviewResult,
    Surface,
    WorkItem,
)
from compliance_review.review.models import validate_review_result_assignment


def parse_review_result(
    content: str, work_item: WorkItem, attempt_id: str, agent_id: str
) -> ReviewResult:
    """Normalize the small set of accepted model result shapes into review_result.v1."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid structured review result: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invalid structured review result: expected JSON object")

    wrapped = payload.get("review_result.v1")
    if isinstance(wrapped, dict):
        payload = wrapped

    try:
        result = ReviewResult.model_validate(payload)
    except ValueError:
        result = _normalize_single_control_result(payload, work_item, attempt_id, agent_id)

    try:
        validate_review_result_assignment(result, work_item, attempt_id, agent_id)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return result


def _normalize_single_control_result(
    payload: dict[str, Any], work_item: WorkItem, attempt_id: str, agent_id: str
) -> ReviewResult:
    if payload.get("schema") != "review_result.v1" and payload.get(
        "review_result_version"
    ) != "review_result.v1":
        raise RuntimeError("invalid structured review result: unsupported JSON shape")
    control_id = payload.get("control_id")
    if control_id is None and len(work_item.control_ids) == 1:
        control_id = work_item.control_ids[0]
    surface = payload.get("surface", work_item.surface)
    if not isinstance(control_id, str) or not isinstance(surface, str):
        raise RuntimeError("invalid structured review result: missing control_id or surface")

    status = str(payload.get("status", "insufficient_evidence")).lower()
    status_map = {
        "pass": "pass",
        "compliant": "pass",
        "fail": "fail",
        "non_compliant": "fail",
        "not_applicable": "not_applicable",
        "waived": "waived",
    }
    recommended_status = status_map.get(status, "indeterminate")
    evidence_status = (
        "complete"
        if recommended_status in {"pass", "fail"}
        else "missing"
        if status in {"insufficient_evidence", "unknown", "indeterminate"}
        else "partial"
    )
    observations: list[str] = []
    assessment = payload.get("assessment") or payload.get("summary")
    if isinstance(assessment, str) and assessment.strip():
        observations.append(assessment.strip())
    findings = payload.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, str) and finding.strip():
                observations.append(finding.strip())
            elif isinstance(finding, dict):
                title = finding.get("title") or finding.get("type") or "finding"
                detail = finding.get("detail") or finding.get("description") or ""
                text = f"{title}: {detail}" if detail else str(title)
                if text.strip():
                    observations.append(text.strip())
                evidence = finding.get("evidence")
                if isinstance(evidence, list):
                    for item in evidence:
                        if not isinstance(item, dict):
                            continue
                        path = item.get("path")
                        lines = item.get("lines") or item.get("line")
                        if isinstance(path, str) and isinstance(lines, (str, int)):
                            observations.append(f"Evidence anchor: {path}:{lines}")
    action = payload.get("recommended_action")
    if isinstance(action, str) and action.strip():
        observations.append(f"Recommended action: {action.strip()}")
    limitations = payload.get("limitations", [])
    gap_reasons = [item.strip() for item in limitations if isinstance(item, str) and item.strip()]
    if evidence_status == "missing" and "insufficient evidence" not in gap_reasons:
        gap_reasons.append("insufficient evidence")
    row = ControlSurfaceResult(
        control_id=control_id,
        surface=cast(Surface, surface),
        evidence_status=cast(EvidenceStatus, evidence_status),
        recommended_control_status=cast(ControlStatus, recommended_status),
        confidence="medium",
        gap_reasons=gap_reasons,
        observations=observations,
    )
    return ReviewResult(
        contract="review_result.v1",
        work_item_id=work_item.work_item_id,
        attempt_id=attempt_id,
        execution_status="completed",
        rows=[row],
        agent_id=agent_id,
    )
