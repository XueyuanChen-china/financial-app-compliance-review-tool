"""Read-only repository access and Git metadata."""

from compliance_review.repository.git import (
    GitMetadata,
    GitRepository,
    is_generated_repository_artifact,
    is_repository_metadata,
)
from compliance_review.repository.sandbox import RepositorySandbox, SandboxViolation
from compliance_review.repository.tools import ReadOnlyRepositoryTools, SearchMatch

__all__ = [
    "GitMetadata",
    "GitRepository",
    "is_generated_repository_artifact",
    "is_repository_metadata",
    "ReadOnlyRepositoryTools",
    "RepositorySandbox",
    "SandboxViolation",
    "SearchMatch",
]
