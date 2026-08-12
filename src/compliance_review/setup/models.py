from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field, model_validator

from compliance_review.domain.models import Confidence, ContractModel, Fact, Surface

ProvenanceSource = Literal[
    "declared",
    "deterministic",
    "inferred",
    "human_confirmed",
    "unresolved",
]
SurfaceStatus = Literal["confirmed", "unresolved"]
ProfileStatus = Literal["draft", "awaiting_confirmation", "confirmed"]


class WorkspaceRepository(ContractModel):
    repo_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    path: str = Field(min_length=1)
    declared_surface: Optional[Surface] = None


class WorkspaceMaterial(ContractModel):
    path: str = Field(min_length=1)
    source_family: str = Field(default="other", min_length=1)


class ComplianceWorkspace(ContractModel):
    contract: Literal["workspace.v1"] = "workspace.v1"
    workspace_root: str = Field(min_length=1)
    repositories: list[WorkspaceRepository] = Field(default_factory=list)
    materials: list[WorkspaceMaterial] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> "ComplianceWorkspace":
        repo_ids = [repo.repo_id for repo in self.repositories]
        if len(repo_ids) != len(set(repo_ids)):
            raise ValueError("workspace repository repo_id values must be unique")
        return self


class DetectionSignal(ContractModel):
    signal_type: str = Field(min_length=1)
    path: Optional[str] = None
    detail: str = Field(min_length=1)


class RepositoryInventory(ContractModel):
    contract: Literal["repository_inventory.v1"] = "repository_inventory.v1"
    repo_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    declared_surface: Optional[Surface] = None
    detected_surface: Optional[Surface] = None
    detected_surfaces: list[Surface] = Field(default_factory=list)
    surface_status: SurfaceStatus
    detection_signals: list[DetectionSignal] = Field(default_factory=list)
    git_revision: Optional[str] = None
    is_git_repository: bool = False
    is_dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)
    git_error_code: Optional[str] = None


class AppFactSet(ContractModel):
    contract: Literal["app_fact_set.v1"] = "app_fact_set.v1"
    facts: list[Fact] = Field(default_factory=list)
    inventory_ids: list[str] = Field(default_factory=list)
    collector_results: list[dict[str, Any]] = Field(default_factory=list)


class ProfileEvidence(ContractModel):
    path: Optional[str] = None
    symbol: Optional[str] = None
    fact_id: Optional[str] = None
    summary: str = Field(min_length=1)


class AppProfileField(ContractModel):
    value: Any = None
    source: ProvenanceSource
    confidence: Confidence
    evidence: list[ProfileEvidence] = Field(default_factory=list)


class AppProfile(ContractModel):
    contract: Literal["app_profile.v1"] = "app_profile.v1"
    version: str = Field(min_length=1)
    status: ProfileStatus
    fields: dict[str, AppProfileField] = Field(default_factory=dict)

    def value_for(self, field_name: str, default: Any = None) -> Any:
        field = self.fields.get(field_name)
        return field.value if field is not None else default


class ProfileConfirmation(ContractModel):
    contract: Literal["app_profile_confirmation.v1"] = "app_profile_confirmation.v1"
    status: Literal["awaiting_confirmation", "confirmed"]
    required_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    confirmed_fields: list[str] = Field(default_factory=list)


class ProfileValidationResult(ContractModel):
    contract: Literal["profile_validation.v1"] = "profile_validation.v1"
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
