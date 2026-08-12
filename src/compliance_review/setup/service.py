from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from compliance_review.persistence import ArtifactStore
from compliance_review.repository import RepositorySandbox
from compliance_review.review.provider import ModelProvider
from compliance_review.setup.app_facts import collect_app_facts
from compliance_review.setup.models import (
    AppFactSet,
    AppProfile,
    AppProfileField,
    ComplianceWorkspace,
    ProfileConfirmation,
    ProfileEvidence,
    ProfileValidationResult,
    RepositoryInventory,
    WorkspaceMaterial,
    WorkspaceRepository,
)
from compliance_review.setup.profile import (
    ProfileAgent,
    ProfileValidator,
    build_profile_draft,
)
from compliance_review.setup.repository_inventory import build_repository_inventory


@dataclass(frozen=True)
class ReviewSetupResult:
    workspace: ComplianceWorkspace
    inventories: list[RepositoryInventory]
    app_facts: AppFactSet
    profile: AppProfile
    profile_validation: ProfileValidationResult
    confirmation: ProfileConfirmation


class ReviewSetupService:
    """Build the deterministic Phase 1 setup artifacts for a Workspace."""

    def __init__(self, workspace_root: Path, profile_provider: ModelProvider | None = None) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.store = ArtifactStore(self.workspace_root)
        self.profile_provider = profile_provider

    def initialize(
        self,
        repositories: Sequence[WorkspaceRepository],
        materials: Sequence[WorkspaceMaterial] = (),
    ) -> ReviewSetupResult:
        if not repositories:
            raise ValueError("at least one repository is required")
        workspace = ComplianceWorkspace(
            workspace_root=self.workspace_root.as_posix(),
            repositories=list(repositories),
            materials=list(materials),
        )
        inventories = [build_repository_inventory(repository) for repository in repositories]
        app_facts = collect_app_facts(inventories)
        profile = build_profile_draft(inventories, app_facts)
        if self.profile_provider is not None and len(inventories) == 1:
            inventory = inventories[0]
            sandbox = RepositorySandbox(Path(inventory.path))
            profile = ProfileAgent(self.profile_provider).run(
                inventory, app_facts, sandbox
            ).model_copy(update={"status": "draft"})
        validation = ProfileValidator().validate(profile, inventories, app_facts)
        unresolved = [
            field_name
            for field_name, field in profile.fields.items()
            if field.source == "unresolved"
        ]
        confirmation = ProfileConfirmation(
            status="awaiting_confirmation" if unresolved or validation.conflicts else "confirmed",
            required_fields=unresolved,
            conflicts=validation.conflicts,
            confirmed_fields=[],
        )
        self.store.write_workspace(workspace)
        self.store.write_repository_inventory(inventories)
        self.store.write_app_facts(app_facts)
        self.store.write_profile_draft(profile)
        self.store.write_profile_validation(validation)
        self.store.write_profile_confirmation(confirmation)
        return ReviewSetupResult(
            workspace=workspace,
            inventories=inventories,
            app_facts=app_facts,
            profile=profile,
            profile_validation=validation,
            confirmation=confirmation,
        )

    def confirm_profile(self, values: dict[str, object]) -> AppProfile:
        """Apply explicit human values and persist the confirmed profile."""
        draft_path = self.workspace_root / "setup" / "app_profile_draft.json"
        if not draft_path.is_file():
            raise ValueError("app_profile_draft.json does not exist")
        profile = AppProfile.model_validate(
            json.loads(draft_path.read_text(encoding="utf-8"))
        )
        fields = dict(profile.fields)
        for field_name, value in values.items():
            existing = fields.get(field_name)
            fields[field_name] = (
                existing.model_copy(
                    update={
                        "value": value,
                        "source": "human_confirmed",
                        "confidence": "high",
                    }
                )
                if existing is not None
                else AppProfileField(
                    value=value,
                    source="human_confirmed",
                    confidence="high",
                    evidence=[ProfileEvidence(summary="value supplied during confirmation")],
                )
            )
        missing = []
        for field_name in ProfileValidator.required_fields:
            field = fields.get(field_name)
            if field is None or field.source == "unresolved":
                missing.append(field_name)
        if missing:
            raise ValueError(f"profile confirmation is missing fields: {sorted(missing)}")
        confirmed = profile.model_copy(update={"status": "confirmed", "fields": fields})
        self.store.write_app_profile(confirmed)
        self.store.write_profile_confirmation(
            ProfileConfirmation(
                status="confirmed",
                required_fields=[],
                conflicts=[],
                confirmed_fields=sorted(values),
            )
        )
        return confirmed
