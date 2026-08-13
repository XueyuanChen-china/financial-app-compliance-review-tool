from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Sequence

from pydantic import TypeAdapter

from compliance_review.compilation.models import ControlValidationResult
from compliance_review.domain.models import (
    ApplicabilityProfile,
    ApplicabilitySet,
    ControlSet,
    CoverageSet,
    ReviewMode,
    Surface,
    WorkItem,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.repository import RepositorySandbox
from compliance_review.review.models import ExcludedControl, ReviewManifest
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
from compliance_review.setup.planning import (
    ApplicabilityEngine,
    CoverageUnitBuilder,
    WorkItemPlanner,
)
from compliance_review.setup.profile import (
    ProfileAgent,
    ProfileValidator,
    build_profile_draft,
    merge_profile_candidate,
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
    applicability_profile: Optional[ApplicabilityProfile] = None
    applicability: Optional[ApplicabilitySet] = None
    coverage: Optional[CoverageSet] = None
    manifest: Optional[ReviewManifest] = None
    run_id: Optional[str] = None
    work_items: list[WorkItem] = field(default_factory=list)
    sandboxes: dict[Surface, RepositorySandbox] = field(default_factory=dict)


class ReviewSetupError(ValueError):
    """Raised when Phase 3 cannot produce a safe Runtime handoff."""


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
        if self.profile_provider is not None:
            sandboxes = {
                inventory.repo_id: RepositorySandbox(Path(inventory.path))
                for inventory in inventories
            }
            candidate = ProfileAgent(self.profile_provider).run_workspace(
                inventories, app_facts, sandboxes
            )
            profile = merge_profile_candidate(profile, candidate)
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

    def confirm_profile(
        self,
        values: dict[str, object],
        repository_surfaces: dict[str, str] | None = None,
    ) -> AppProfile:
        """Apply explicit human values and persist the confirmed profile."""
        draft_path = self.workspace_root / "setup" / "app_profile_draft.json"
        if not draft_path.is_file():
            raise ValueError("app_profile_draft.json does not exist")
        profile = AppProfile.model_validate(
            json.loads(draft_path.read_text(encoding="utf-8"))
        )
        inventory_path = self.workspace_root / "setup" / "repository_inventory.json"
        facts_path = self.workspace_root / "setup" / "app_facts.json"
        if not inventory_path.is_file() or not facts_path.is_file():
            raise ValueError("repository inventory and app facts are required for confirmation")
        inventories = [
            RepositoryInventory.model_validate(item)
            for item in json.loads(inventory_path.read_text(encoding="utf-8"))
        ]
        app_facts = AppFactSet.model_validate(
            json.loads(facts_path.read_text(encoding="utf-8"))
        )
        if repository_surfaces:
            inventories = self._confirm_repository_surfaces(
                inventories, repository_surfaces
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
            if field is None or field.value is None or field.value == "unknown":
                missing.append(field_name)
        confirmed = profile.model_copy(update={"status": "confirmed", "fields": fields})
        validation = ProfileValidator().validate(confirmed, inventories, app_facts)
        self.store.write_repository_inventory(inventories)
        self.store.write_profile_validation(validation)
        if missing or not validation.valid:
            confirmation = ProfileConfirmation(
                status="awaiting_confirmation",
                required_fields=sorted(missing),
                conflicts=validation.conflicts,
                confirmed_fields=sorted(values),
            )
            self.store.write_profile_confirmation(confirmation)
            details = [*validation.errors, *validation.conflicts]
            if missing:
                details.append(f"profile confirmation is missing fields: {sorted(missing)}")
            raise ValueError("; ".join(details))
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

    def compile(
        self,
        workspace: Optional[ComplianceWorkspace] = None,
        run_id: Optional[str] = None,
        mode: ReviewMode = "full",
        max_concurrency: int = 3,
    ) -> ReviewSetupResult:
        """Compile confirmed setup state into Coverage Units and Runtime Work Items."""
        workspace = workspace or self._load_workspace()
        if not workspace.repositories:
            raise ReviewSetupError("at least one workspace repository is required")
        if not 1 <= max_concurrency <= 32:
            raise ReviewSetupError("max_concurrency must be between 1 and 32")

        inventories = [build_repository_inventory(repo) for repo in workspace.repositories]
        app_facts = collect_app_facts(inventories)
        profile = self._load_confirmed_profile()
        profile_validation = ProfileValidator().validate(profile, inventories, app_facts)
        if profile.status != "confirmed":
            raise ReviewSetupError("confirmed AppProfile is required before review setup")
        if not profile_validation.valid:
            raise ReviewSetupError(
                "confirmed AppProfile failed deterministic validation: "
                + "; ".join([*profile_validation.errors, *profile_validation.conflicts])
            )

        controls, control_validation = self._load_validated_controls()
        applicability_profile = _to_applicability_profile(profile, inventories)
        applicability = ApplicabilityEngine().evaluate(applicability_profile, controls)
        coverage = CoverageUnitBuilder().build(
            applicability_profile, controls, applicability
        )
        selected_run_id = run_id or _new_run_id()
        run_paths = self.store.prepare_run_artifacts(selected_run_id)
        plan = WorkItemPlanner().plan(
            applicability_profile,
            controls,
            coverage,
            app_facts,
            inventories,
            run_paths["run_root"],
        )
        manifest = ReviewManifest(
            contract="review_manifest.v1",
            run_id=selected_run_id,
            mode=mode,
            default_max_concurrency=max_concurrency,
            surface_roots={
                surface: sandbox.root.as_posix()
                for surface, sandbox in plan.sandboxes.items()
            },
            work_items=plan.work_items,
            excluded_controls=[
                ExcludedControl(
                    control_id=control_id,
                    reason="applicability expression evaluated false",
                )
                for control_id in plan.coverage.excluded_control_ids
            ],
            coverage_unit_ids=[unit.coverage_unit_id for unit in plan.coverage.units],
            unknown_control_ids=plan.coverage.unknown_control_ids,
            missing_surfaces=plan.coverage.missing_surfaces,
            source_profile_version=applicability_profile.version,
            source_control_version=controls.version,
        )
        self.store.write_workspace(workspace)
        self.store.write_repository_inventory(inventories)
        self.store.write_app_facts(app_facts)
        self.store.write_applicability(applicability)
        self.store.write_coverage_units(plan.coverage)
        self.store.write_review_manifest(selected_run_id, manifest)
        return ReviewSetupResult(
            workspace=workspace,
            inventories=inventories,
            app_facts=app_facts,
            profile=profile,
            profile_validation=profile_validation,
            confirmation=ProfileConfirmation(
                status="confirmed", required_fields=[], conflicts=[], confirmed_fields=[]
            ),
            applicability_profile=applicability_profile,
            applicability=applicability,
            coverage=plan.coverage,
            manifest=manifest,
            run_id=selected_run_id,
            work_items=plan.work_items,
            sandboxes=plan.sandboxes,
        )

    def _load_workspace(self) -> ComplianceWorkspace:
        path = self.workspace_root / "workspace.json"
        if not path.is_file():
            raise ReviewSetupError(f"workspace.json does not exist: {path}")
        try:
            return ComplianceWorkspace.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(f"invalid workspace.json: {exc}") from exc

    def _load_confirmed_profile(self) -> AppProfile:
        path = self.workspace_root / "setup" / "app_profile.json"
        if not path.is_file():
            raise ReviewSetupError(
                "confirmed AppProfile is missing at setup/app_profile.json; "
                "confirm the AppProfile first"
            )
        try:
            return AppProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(f"invalid confirmed AppProfile: {exc}") from exc

    def _load_validated_controls(self) -> tuple[ControlSet, ControlValidationResult]:
        controls_path = self.workspace_root / "setup" / "controls.json"
        validation_path = self.workspace_root / "setup" / "control_validation.json"
        if not controls_path.is_file() or not validation_path.is_file():
            raise ReviewSetupError(
                "validated controls and control_validation.json are required before review setup"
            )
        try:
            controls = ControlSet.model_validate(
                json.loads(controls_path.read_text(encoding="utf-8"))
            )
            validation = ControlValidationResult.model_validate(
                json.loads(validation_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(f"invalid validated Control Set artifacts: {exc}") from exc
        if not validation.valid:
            raise ReviewSetupError(
                "validated Control Set is unavailable because deterministic validation failed"
            )
        if validation.validated_control_count != len(controls.controls):
            raise ReviewSetupError(
                "control_validation count does not match setup/controls.json"
            )
        return controls, validation

    @staticmethod
    def _confirm_repository_surfaces(
        inventories: list[RepositoryInventory], selections: dict[str, str]
    ) -> list[RepositoryInventory]:
        updated: list[RepositoryInventory] = []
        for inventory in inventories:
            selected = selections.get(inventory.repo_id)
            if selected is None:
                updated.append(inventory)
                continue
            if selected not in inventory.detected_surfaces:
                raise ValueError(
                    f"repository surface must be one of detected surfaces for "
                    f"{inventory.repo_id}: {inventory.detected_surfaces}"
                )
            updated.append(
                inventory.model_copy(
                    update={
                        "declared_surface": selected,
                        "detected_surface": selected,
                        "surface_status": "confirmed",
                    }
                )
            )
        unknown_ids = set(selections) - {inventory.repo_id for inventory in inventories}
        if unknown_ids:
            raise ValueError(f"unknown repository ids: {sorted(unknown_ids)}")
        return updated


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:16]}"


def _to_applicability_profile(
    profile: AppProfile, inventories: Sequence[RepositoryInventory]
) -> ApplicabilityProfile:
    def required_value(name: str) -> object:
        field = profile.fields.get(name)
        if field is None or field.value is None or field.value == "unknown":
            raise ReviewSetupError(f"confirmed AppProfile field is unresolved: {name}")
        return field.value

    raw_business_type = required_value("business_type")
    business_type = (
        [raw_business_type]
        if isinstance(raw_business_type, str)
        else list(raw_business_type)
        if isinstance(raw_business_type, list)
        else None
    )
    if not business_type or not all(isinstance(item, str) and item for item in business_type):
        raise ReviewSetupError("AppProfile business_type must be a non-empty string list")
    raw_surfaces = required_value("evidence_surfaces")
    if not isinstance(raw_surfaces, list) or not raw_surfaces:
        raise ReviewSetupError("AppProfile evidence_surfaces must be a non-empty list")
    try:
        surfaces = TypeAdapter(list[Surface]).validate_python(raw_surfaces)
    except (TypeError, ValueError) as exc:
        raise ReviewSetupError(f"invalid AppProfile evidence_surfaces: {exc}") from exc

    roots: dict[Surface, str] = {}
    raw_roots = profile.value_for("repository_roots", {})
    if isinstance(raw_roots, dict):
        for raw_surface, raw_root in raw_roots.items():
            if not isinstance(raw_surface, str):
                continue
            try:
                surface: Surface = TypeAdapter(Surface).validate_python(raw_surface)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_surface, str) and isinstance(raw_root, list) and raw_root:
                roots[surface] = str(raw_root[0])
            elif isinstance(raw_surface, str) and isinstance(raw_root, str):
                roots[surface] = raw_root
    for inventory in inventories:
        inventory_surface = inventory.detected_surface or inventory.declared_surface
        if inventory_surface is not None:
            roots.setdefault(inventory_surface, inventory.path)
    raw_self_lending = required_value("self_lending")
    if isinstance(raw_self_lending, bool):
        self_lending: bool | Literal["unknown"] = raw_self_lending
    elif raw_self_lending == "unknown":
        self_lending = "unknown"
    else:
        raise ReviewSetupError("AppProfile self_lending must be true, false, or unknown")
    review_scope: Literal[
        "full_release_package", "multi_surface_static_review", "partial"
    ] = profile.value_for("review_scope", "partial")
    if review_scope not in {"full_release_package", "multi_surface_static_review", "partial"}:
        review_scope = "partial"
    try:
        return ApplicabilityProfile(
            contract="applicability_profile.v1",
            version=profile.version,
            app_name=str(required_value("app_name")),
            package_name=str(required_value("package_name")),
            jurisdiction=str(required_value("jurisdiction")),
            business_type=business_type,
            self_lending=self_lending,
            evidence_surfaces=surfaces,
            review_scope=review_scope,
            roots=roots,
        )
    except ValueError as exc:
        raise ReviewSetupError(
            "confirmed AppProfile cannot become applicability profile: " + str(exc)
        ) from exc
