"""Restricted persistence for validated Workspace artifacts."""

from compliance_review.persistence.artifact_store import ArtifactStore, WorkspacePathViolation

__all__ = ["ArtifactStore", "WorkspacePathViolation"]
