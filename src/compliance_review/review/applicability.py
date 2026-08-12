from __future__ import annotations

import re

from compliance_review.domain.models import ApplicabilityProfile, Control

_EQUALS_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<value>.+)$")
_INCLUDES_RE = re.compile(
    r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+includes\s+(?P<value>.+)$"
)
_IN_RE = re.compile(
    r"^(?P<value>[A-Za-z0-9_.-]+)\s+in\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)$"
)


def control_applicability(control: Control, profile: ApplicabilityProfile) -> bool | None:
    """Evaluate the small, declarative expression language used by MVP controls.

    ``None`` means the expression is unsupported or the profile lacks enough data.
    The builder handles it conservatively by retaining the control in the manifest.
    """
    expression = control.applicability_expression.strip()
    if not expression:
        return None
    clauses = re.split(r"\s+(?:and|&&)\s+", expression, flags=re.IGNORECASE)
    results = [_evaluate_clause(clause.strip(), profile) for clause in clauses]
    if any(result is None for result in results):
        return None
    return all(result is True for result in results)


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
