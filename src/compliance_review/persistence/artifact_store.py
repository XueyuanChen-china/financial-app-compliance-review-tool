from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from compliance_review.setup.models import (
    AppFactSet,
    AppProfile,
    ComplianceWorkspace,
    ProfileConfirmation,
    ProfileValidationResult,
    RepositoryInventory,
)


class WorkspacePathViolation(ValueError):
    """Raised when an artifact target escapes the Workspace root."""


class ArtifactStore:
    """Write only known Workspace artifacts with atomic, confined JSON writes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_workspace(self, workspace: ComplianceWorkspace) -> Path:
        return self._write_model("workspace.json", workspace)

    def write_repository_inventory(
        self, inventories: list[RepositoryInventory]
    ) -> Path:
        return self._write_json(
            "setup/repository_inventory.json",
            [inventory.model_dump(mode="json") for inventory in inventories],
        )

    def write_app_facts(self, facts: AppFactSet) -> Path:
        return self._write_model("setup/app_facts.json", facts)

    def write_profile_draft(self, profile: AppProfile) -> Path:
        return self._write_model("setup/app_profile_draft.json", profile)

    def write_profile_confirmation(self, confirmation: ProfileConfirmation) -> Path:
        return self._write_model("setup/app_profile_confirmation.json", confirmation)

    def write_app_profile(self, profile: AppProfile) -> Path:
        if profile.status != "confirmed":
            raise ValueError("only confirmed profiles can be written as app_profile.json")
        return self._write_model("setup/app_profile.json", profile)

    def write_profile_validation(self, result: ProfileValidationResult) -> Path:
        return self._write_model("setup/profile_validation.json", result)

    def _write_model(self, relative_path: str, value: BaseModel) -> Path:
        return self._write_json(relative_path, value.model_dump(mode="json"))

    def _write_json(self, relative_path: str, value: Any) -> Path:
        target = self._confined_target(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    def _confined_target(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise WorkspacePathViolation(f"artifact path must be relative: {relative_path}")
        target = (self.root / candidate).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathViolation(
                f"artifact path leaves workspace root: {relative_path}"
            ) from exc
        return target
