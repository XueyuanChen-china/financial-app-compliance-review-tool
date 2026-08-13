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
from compliance_review.domain.models import Control, ControlSet, SourceRef

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
        source_map, source_errors = _source_registry_maps(registry)
        errors.extend(source_errors)

        obligation_ids = [item.obligation_id for item in obligations.obligations]
        duplicate_obligation_ids = sorted(
            item_id for item_id, count in Counter(obligation_ids).items() if count > 1
        )
        errors.extend(
            f"duplicate obligation_id: {obligation_id}"
            for obligation_id in duplicate_obligation_ids
        )
        obligation_map = {item.obligation_id: item for item in obligations.obligations}

        control_ids = [draft.control_id for draft in drafts.controls]
        duplicate_control_ids = sorted(
            control_id for control_id, count in Counter(control_ids).items() if count > 1
        )
        errors.extend(f"duplicate control_id: {control_id}" for control_id in duplicate_control_ids)
        group_keys = [
            (_normalize(draft.module_id), _normalize(draft.title)) for draft in drafts.controls
        ]
        duplicate_groups = sorted(
            f"{module}:{title}"
            for (module, title), count in Counter(group_keys).items()
            if count > 1
        )
        warnings.extend(f"possible duplicate control group: {group}" for group in duplicate_groups)
        if not drafts.controls:
            errors.append("control draft set is empty")

        covered_sections: set[tuple[str, str]] = set()
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
            else:
                covered_sections.add((obligation.source_id, obligation.source_section))
            errors.extend(
                f"obligation {obligation.obligation_id}: {message}"
                for message in validate_applicability_expression(
                    obligation.applicability_expression
                )
            )
            primary_ref = (obligation.source_id, obligation.source_section)
            obligation_ref_keys = _source_ref_keys(obligation.source_refs)
            if primary_ref not in obligation_ref_keys:
                errors.append(
                    f"obligation {obligation.obligation_id} must include its primary source ref"
                )
            for source_ref in obligation.source_refs:
                errors.extend(
                    _validate_source_ref(
                        source_ref,
                        source_map,
                        f"obligation {obligation.obligation_id}",
                        require_section=True,
                    )
                )

        for source in registry.sources:
            for section in source.sections:
                if (
                    source.source_id,
                    section.section_id,
                ) not in covered_sections:
                    errors.append(
                        f"source section is not covered by an obligation: "
                        f"{source.source_id}/{section.section_id}"
                    )

        linked_obligation_ids = {
            obligation_id for draft in drafts.controls for obligation_id in draft.obligation_ids
        }
        for obligation in obligations.obligations:
            if obligation.obligation_id not in linked_obligation_ids:
                errors.append(f"obligation has no mapped control: {obligation.obligation_id}")

        for draft in drafts.controls:
            linked_obligations = []
            for obligation_id in draft.obligation_ids:
                linked_obligation = obligation_map.get(obligation_id)
                if linked_obligation is None:
                    errors.append(
                        f"control {draft.control_id} references unknown obligation: {obligation_id}"
                    )
                else:
                    linked_obligations.append(linked_obligation)
            errors.extend(
                f"control {draft.control_id}: {message}"
                for message in validate_applicability_expression(draft.applicability_expression)
            )
            if set(draft.required_surfaces) != set(draft.evidence_requirements):
                errors.append(
                    f"control {draft.control_id} evidence requirements must cover exactly "
                    "required surfaces"
                )
            if not draft.evidence_requirements:
                errors.append(f"control {draft.control_id} has no evidence requirements")
            for source_ref in draft.source_refs:
                errors.extend(
                    _validate_source_ref(
                        source_ref,
                        source_map,
                        f"control {draft.control_id}",
                        require_section=True,
                    )
                )
            draft_ref_keys = _source_ref_keys(draft.source_refs)
            obligation_ref_keys = {
                key
                for obligation in linked_obligations
                for key in _source_ref_keys(obligation.source_refs)
            }
            missing_provenance = sorted(obligation_ref_keys - draft_ref_keys)
            if missing_provenance:
                errors.append(
                    f"control {draft.control_id} does not preserve obligation provenance: "
                    + ", ".join(f"{source}/{section}" for source, section in missing_provenance)
                )
            for obligation in linked_obligations:
                if not _applicability_is_no_narrower(
                    obligation.applicability_expression,
                    draft.applicability_expression,
                ):
                    errors.append(
                        f"control {draft.control_id} narrows applicability of obligation "
                        f"{obligation.obligation_id}"
                    )
                missing_surfaces = set(obligation.required_surfaces) - set(draft.required_surfaces)
                if missing_surfaces:
                    errors.append(
                        f"control {draft.control_id} narrows required surfaces of obligation "
                        f"{obligation.obligation_id}: {sorted(missing_surfaces)}"
                    )

        return ControlValidationResult(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            duplicate_obligation_ids=duplicate_obligation_ids,
            duplicate_control_ids=duplicate_control_ids,
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
        field = match.group("field") or match.group("include_field") or match.group("in_field")
        if field not in _ALLOWED_FIELDS:
            errors.append(f"unsupported applicability field: {field}")
    return errors


def _source_registry_maps(
    registry: SourceRegistry,
) -> tuple[dict[str, ComplianceSource], list[str]]:
    errors: list[str] = []
    source_ids = [source.source_id for source in registry.sources]
    duplicate_source_ids = sorted(
        source_id for source_id, count in Counter(source_ids).items() if count > 1
    )
    errors.extend(f"duplicate source_id: {source_id}" for source_id in duplicate_source_ids)
    source_map = {source.source_id: source for source in registry.sources}
    for source in registry.sources:
        section_ids = [section.section_id for section in source.sections]
        duplicate_sections = sorted(
            section_id for section_id, count in Counter(section_ids).items() if count > 1
        )
        errors.extend(
            f"duplicate source section: {source.source_id}/{section_id}"
            for section_id in duplicate_sections
        )
        ordinals = [section.ordinal for section in source.sections]
        if len(ordinals) != len(set(ordinals)):
            errors.append(f"duplicate source section ordinal: {source.source_id}")
    return source_map, errors


def _validate_source_ref(
    source_ref: SourceRef,
    source_map: dict[str, ComplianceSource],
    owner: str,
    *,
    require_section: bool,
) -> list[str]:
    errors: list[str] = []
    if source_ref.source_id is None:
        errors.append(f"{owner} has source ref without source_id")
        return errors
    source = source_map.get(source_ref.source_id)
    if source is None:
        errors.append(f"{owner} references unknown source: {source_ref.source_id}")
        return errors
    if require_section and source_ref.source_section is None:
        errors.append(f"{owner} has source ref without source_section")
    elif source_ref.source_section and not _has_section(source, source_ref.source_section):
        errors.append(
            f"{owner} references unknown source section: "
            f"{source_ref.source_id}/{source_ref.source_section}"
        )
    return errors


def _source_ref_keys(refs: list[SourceRef]) -> set[tuple[str, str]]:
    return {
        (ref.source_id, ref.source_section)
        for ref in refs
        if ref.source_id is not None and ref.source_section is not None
    }


def _applicability_is_no_narrower(obligation: str, control: str) -> bool:
    obligation_clauses = _normalized_clauses(obligation)
    control_clauses = _normalized_clauses(control)
    if obligation_clauses is None or control_clauses is None:
        return obligation.strip() == control.strip()
    return control_clauses.issubset(obligation_clauses)


def _normalized_clauses(expression: str) -> set[str] | None:
    if validate_applicability_expression(expression):
        return None
    return {
        re.sub(r"\s+", " ", clause.strip()).lower()
        for clause in re.split(r"\s+(?:and|&&)\s+", expression.strip(), flags=re.IGNORECASE)
    }


def _has_section(source: ComplianceSource, section_id: str) -> bool:
    return any(section.section_id == section_id for section in source.sections)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
