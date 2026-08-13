from __future__ import annotations

import re
from collections import Counter

from compliance_review.compilation.models import (
    ComplianceSource,
    ControlDraftSet,
    ControlValidationResult,
    ObligationSet,
    SourceRegistry,
)
from compliance_review.domain.models import Control, ControlSet

_CLAUSE_RE = re.compile(
    r"^(?:(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<equals>[^\s]+)|"
    r"(?P<include_field>[A-Za-z_][A-Za-z0-9_]*)\s+includes\s+(?P<include>[^\s]+)|"
    r"(?P<value>[A-Za-z0-9_.-]+)\s+in\s+(?P<in_field>[A-Za-z_][A-Za-z0-9_]*))$"
)
_ALLOWED_FIELDS = {"business_type", "evidence_surfaces", "self_lending", "jurisdiction"}


class ControlValidator:
    """Validate Phase 2 draft controls without using an LLM."""

    def validate(
        self,
        registry: SourceRegistry,
        obligations: ObligationSet,
        drafts: ControlDraftSet,
    ) -> ControlValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        obligation_map = {item.obligation_id: item for item in obligations.obligations}
        source_map = {source.source_id: source for source in registry.sources}
        control_ids = [draft.control_id for draft in drafts.controls]
        duplicate_ids = sorted(
            control_id for control_id, count in Counter(control_ids).items() if count > 1
        )
        errors.extend(f"duplicate control_id: {control_id}" for control_id in duplicate_ids)
        group_keys = [
            (_normalize(draft.module_id), _normalize(draft.title))
            for draft in drafts.controls
        ]
        duplicate_groups = sorted(
            f"{module}:{title}"
            for (module, title), count in Counter(group_keys).items()
            if count > 1
        )
        warnings.extend(f"possible duplicate control group: {group}" for group in duplicate_groups)
        if not drafts.controls:
            errors.append("control draft set is empty")

        for obligation in obligations.obligations:
            source = source_map.get(obligation.source_id)
            if source is None:
                errors.append(
                    f"obligation {obligation.obligation_id} references unknown source: "
                    f"{obligation.source_id}"
                )
            elif not _has_section(source, obligation.source_section):
                errors.append(
                    f"obligation {obligation.obligation_id} references unknown source section: "
                    f"{obligation.source_id}/{obligation.source_section}"
                )
            for source_ref in obligation.source_refs:
                if source_ref.source_id != obligation.source_id:
                    errors.append(
                        f"obligation {obligation.obligation_id} source ref does not match source_id"
                    )
                elif source_ref.source_section and not _has_section(
                    source_map[obligation.source_id], source_ref.source_section
                ):
                    errors.append(
                        f"obligation {obligation.obligation_id} source ref has unknown section: "
                        f"{source_ref.source_id}/{source_ref.source_section}"
                    )

        for draft in drafts.controls:
            for obligation_id in draft.obligation_ids:
                linked_obligation = obligation_map.get(obligation_id)
                if linked_obligation is None:
                    errors.append(
                        f"control {draft.control_id} references unknown obligation: {obligation_id}"
                    )
            for source_ref in draft.source_refs:
                if source_ref.source_id is None:
                    errors.append(f"control {draft.control_id} has source ref without source_id")
                    continue
                source = source_map.get(source_ref.source_id)
                if source is None:
                    errors.append(
                        f"control {draft.control_id} references unknown source: "
                        f"{source_ref.source_id}"
                    )
                elif source_ref.source_section and not _has_section(
                    source, source_ref.source_section
                ):
                    errors.append(
                        f"control {draft.control_id} references unknown source section: "
                        f"{source_ref.source_id}/{source_ref.source_section}"
                    )
            errors.extend(
                f"control {draft.control_id}: {message}"
                for message in validate_applicability_expression(
                    draft.applicability_expression
                )
            )
            if set(draft.required_surfaces) != set(draft.evidence_requirements):
                errors.append(
                    f"control {draft.control_id} evidence requirements must cover exactly "
                    "required surfaces"
                )
            if not draft.evidence_requirements:
                errors.append(f"control {draft.control_id} has no evidence requirements")
            for obligation_id in draft.obligation_ids:
                linked_obligation = obligation_map.get(obligation_id)
                obligation_refs = {
                    (ref.source_id, ref.source_section)
                    for ref in linked_obligation.source_refs
                } if linked_obligation else set()
                draft_refs = {
                    (ref.source_id, ref.source_section) for ref in draft.source_refs
                }
                if linked_obligation and not obligation_refs.intersection(draft_refs):
                    warnings.append(
                        f"control {draft.control_id} does not repeat an obligation source ref"
                    )
        return ControlValidationResult(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            duplicate_control_ids=duplicate_ids,
            duplicate_control_groups=duplicate_groups,
            validated_control_count=len(drafts.controls) if not errors else 0,
        )

    def to_control_set(
        self,
        drafts: ControlDraftSet,
        validation: ControlValidationResult,
    ) -> ControlSet:
        if not validation.valid:
            raise ValueError("cannot create validated controls from invalid drafts")
        controls = []
        for draft in drafts.controls:
            payload = draft.model_dump()
            payload["minimum_evidence_strength"] = {
                surface: requirement.minimum_strength
                for surface, requirement in draft.evidence_requirements.items()
            }
            controls.append(Control(**payload))
        return ControlSet(contract="control_set.v1", version="1.0", controls=controls)


def validate_applicability_expression(expression: str) -> list[str]:
    if not expression.strip():
        return ["applicability_expression is empty"]
    errors: list[str] = []
    clauses = re.split(r"\s+(?:and|&&)\s+", expression.strip(), flags=re.IGNORECASE)
    for clause in clauses:
        match = _CLAUSE_RE.match(clause.strip())
        if match is None:
            errors.append(f"invalid applicability clause: {clause}")
            continue
        field = (
            match.group("field")
            or match.group("include_field")
            or match.group("in_field")
        )
        if field not in _ALLOWED_FIELDS:
            errors.append(f"unsupported applicability field: {field}")
    return errors


def _has_section(source: ComplianceSource, section_id: str) -> bool:
    return any(section.section_id == section_id for section in source.sections)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
