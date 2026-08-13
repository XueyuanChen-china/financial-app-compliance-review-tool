from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

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

    def write_repository_inventory(self, inventories: list[RepositoryInventory]) -> Path:
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

    def write_source_registry(self, registry: BaseModel) -> Path:
        return self._write_model("setup/sources.json", registry)

    def write_obligations(self, obligations: BaseModel) -> Path:
        return self._write_model("setup/obligations.json", obligations)

    def write_obligation_extraction_batches(self, batches: Sequence[BaseModel]) -> Path:
        return self._write_json(
            "setup/obligation_extraction_batches.json",
            [batch.model_dump(mode="json") for batch in batches],
        )

    def write_controls_draft(self, controls: BaseModel) -> Path:
        return self._write_model("setup/controls_draft.json", controls)

    def write_controls(self, controls: BaseModel) -> Path:
        return self._write_model("setup/controls.json", controls)

    def write_control_validation(self, result: BaseModel) -> Path:
        return self._write_model("setup/control_validation.json", result)

    def write_applicability(self, result: BaseModel) -> Path:
        return self._write_model("setup/applicability.json", result)

    def write_coverage_units(self, result: BaseModel) -> Path:
        return self._write_model("setup/coverage_units.json", result)

    def write_review_manifest(self, run_id: str, manifest: BaseModel) -> Path:
        return self._write_model(f"runs/{run_id}/manifest.json", manifest)

    def write_run_model(self, run_id: str, name: str, value: BaseModel) -> Path:
        return self._write_model(f"runs/{run_id}/{name}", value)

    def write_run_json(self, run_id: str, name: str, value: Any) -> Path:
        return self._write_json(f"runs/{run_id}/{name}", value)

    def write_run_text(self, run_id: str, name: str, value: str) -> Path:
        target = self._confined_target(f"runs/{run_id}/{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(target)
        return target

    def prepare_run_artifacts(self, run_id: str) -> dict[str, Path]:
        """Create the runtime handoff paths without starting Reviewer execution."""
        run_root = self._confined_target(f"runs/{run_id}")
        reviewer_results = run_root / "reviewer_results"
        reviewer_results.mkdir(parents=True, exist_ok=True)
        event_log = run_root / "worker-events.jsonl"
        event_log.touch(exist_ok=True)
        checkpoint = run_root / "checkpoint.sqlite"
        with sqlite3.connect(checkpoint) as connection:
            connection.execute("PRAGMA user_version = 1")
        return {
            "run_root": run_root,
            "reviewer_results": reviewer_results,
            "event_log": event_log,
            "checkpoint": checkpoint,
        }

    def invalidate_controls(self) -> None:
        """Remove only the generated validated control artifact before recompilation."""
        target = self._confined_target("setup/controls.json")
        if target.is_file():
            target.unlink()

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
