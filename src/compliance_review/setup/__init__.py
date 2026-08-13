"""Deterministic review setup and profile preparation components."""

from compliance_review.setup.app_facts import collect_app_facts
from compliance_review.setup.models import (
    AppFactSet,
    AppProfile,
    AppProfileField,
    ComplianceWorkspace,
    ProfileConfirmation,
    ProfileValidationResult,
    RepositoryInventory,
)
from compliance_review.setup.planning import (
    ApplicabilityEngine,
    CoverageUnitBuilder,
    WorkItemPlan,
    WorkItemPlanner,
)
from compliance_review.setup.profile import (
    ProfileAgent,
    ProfileValidator,
    build_profile_draft,
    merge_profile_candidate,
)
from compliance_review.setup.repository_inventory import build_repository_inventory

__all__ = [
    "AppFactSet",
    "AppProfile",
    "AppProfileField",
    "ComplianceWorkspace",
    "ProfileAgent",
    "ProfileConfirmation",
    "ProfileValidationResult",
    "ProfileValidator",
    "RepositoryInventory",
    "ReviewSetupResult",
    "ReviewSetupError",
    "ReviewSetupService",
    "ApplicabilityEngine",
    "CoverageUnitBuilder",
    "WorkItemPlan",
    "WorkItemPlanner",
    "build_profile_draft",
    "merge_profile_candidate",
    "build_repository_inventory",
    "collect_app_facts",
]


def __getattr__(name: str) -> object:
    """Load the service lazily to avoid persistence/setup model import cycles."""
    if name in {"ReviewSetupError", "ReviewSetupResult", "ReviewSetupService"}:
        from compliance_review.setup.service import (
            ReviewSetupError,
            ReviewSetupResult,
            ReviewSetupService,
        )

        return {
            "ReviewSetupError": ReviewSetupError,
            "ReviewSetupResult": ReviewSetupResult,
            "ReviewSetupService": ReviewSetupService,
        }[name]
    raise AttributeError(name)
