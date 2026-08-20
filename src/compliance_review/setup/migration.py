"""Explicit read adapters for the v1-to-v2 preparation contract transition."""

from __future__ import annotations

from typing import Any, Mapping, TypeVar

from pydantic import BaseModel

from compliance_review.domain.models import (
    ApplicabilityProfile,
    ApplicabilitySet,
    ControlSet,
    CoverageSet,
)
from compliance_review.review.models import ReviewManifest


class ContractMigrationError(ValueError):
    """Raised when a durable artifact cannot be read as a supported contract."""


ModelT = TypeVar("ModelT", bound=BaseModel)


def adapt_control_set(payload: Mapping[str, Any]) -> ControlSet:
    value = _validate(payload, ControlSet, "Control Set")
    if value.contract == "control_set.v1":
        return value.model_copy(update={"contract": "control_set.v2", "version": "2.0"})
    return value


def adapt_applicability_profile(payload: Mapping[str, Any]) -> ApplicabilityProfile:
    value = _validate(payload, ApplicabilityProfile, "Applicability Profile")
    if value.contract == "applicability_profile.v1":
        return value.model_copy(update={"contract": "applicability_profile.v2", "version": "2.0"})
    return value


def adapt_applicability_set(payload: Mapping[str, Any]) -> ApplicabilitySet:
    value = _validate(payload, ApplicabilitySet, "Applicability Set")
    if value.contract == "applicability_set.v1":
        return value.model_copy(update={"contract": "applicability_set.v2"})
    return value


def adapt_coverage_set(payload: Mapping[str, Any]) -> CoverageSet:
    value = _validate(payload, CoverageSet, "Coverage Set")
    if value.contract == "coverage_set.v1":
        return value.model_copy(update={"contract": "coverage_set.v2"})
    return value


def adapt_review_manifest(payload: Mapping[str, Any]) -> ReviewManifest:
    value = _validate(payload, ReviewManifest, "Review Manifest")
    if value.contract == "review_manifest.v1":
        return value.model_copy(update={"contract": "review_manifest.v2"})
    return value


def _validate(payload: Mapping[str, Any], model: type[ModelT], label: str) -> ModelT:
    try:
        return model.model_validate(dict(payload))
    except (TypeError, ValueError) as exc:
        raise ContractMigrationError(f"invalid {label} contract: {exc}") from exc
