from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Sequence

from pydantic import TypeAdapter

from compliance_review.collectors.base import CollectorResult
from compliance_review.compilation.models import (
    ControlValidationResult,
    Obligation,
    ObligationSet,
    SourceRegistry,
)
from compliance_review.domain.models import (
    ApplicabilityAnswerSet,
    ApplicabilityDecision,
    ApplicabilityProfile,
    ApplicabilityProfileFact,
    ApplicabilityResolution,
    ApplicabilityResolutionCheckpoint,
    ApplicabilitySet,
    ControlSet,
    CoverageSet,
    ReviewMode,
    Surface,
    WorkItem,
)
from compliance_review.persistence import ArtifactStore
from compliance_review.repository import RepositorySandbox
from compliance_review.review.applicability import ApplicabilityResolutionLoop
from compliance_review.review.models import ExcludedControl, ReviewManifest
from compliance_review.review.provider import ModelProvider
from compliance_review.setup.app_facts import collect_app_facts
from compliance_review.setup.migration import adapt_control_set
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
    sandboxes: dict[str, RepositorySandbox] = field(default_factory=dict)
    collector_results: dict[str, CollectorResult] = field(default_factory=dict)
    applicability_resolution: Optional[ApplicabilityResolution] = None


class ReviewSetupError(ValueError):
    """Raised when Phase 3 cannot produce a safe Runtime handoff."""


class ReviewSetupService:
    """Build the deterministic Phase 1 setup artifacts for a Workspace."""

    def __init__(
        self,
        workspace_root: Path,
        profile_provider: ModelProvider | None = None,
        applicability_provider: ModelProvider | None = None,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.store = ArtifactStore(self.workspace_root)
        self.profile_provider = profile_provider
        self.applicability_provider = applicability_provider

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
        app_facts = collect_app_facts(inventories, list(materials))
        profile = build_profile_draft(inventories, app_facts, list(materials))
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
        confirmation_status: Literal[
            "awaiting_confirmation", "confirmed", "deferred_to_applicability"
        ] = (
            "awaiting_confirmation"
            if validation.conflicts
            else "deferred_to_applicability"
            if unresolved
            else "confirmed"
        )
        confirmation = ProfileConfirmation(
            status=confirmation_status,
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
        profile = AppProfile.model_validate(json.loads(draft_path.read_text(encoding="utf-8")))
        inventory_path = self.workspace_root / "setup" / "repository_inventory.json"
        facts_path = self.workspace_root / "setup" / "app_facts.json"
        if not inventory_path.is_file() or not facts_path.is_file():
            raise ValueError("repository inventory and app facts are required for confirmation")
        inventories = [
            RepositoryInventory.model_validate(item)
            for item in json.loads(inventory_path.read_text(encoding="utf-8"))
        ]
        app_facts = AppFactSet.model_validate(json.loads(facts_path.read_text(encoding="utf-8")))
        workspace_path = self.workspace_root / "workspace.json"
        if not workspace_path.is_file():
            raise ValueError("workspace.json does not exist")
        workspace = ComplianceWorkspace.model_validate(
            json.loads(workspace_path.read_text(encoding="utf-8"))
        )
        if repository_surfaces:
            inventories = self._confirm_repository_surfaces(inventories, repository_surfaces)
            workspace = workspace.model_copy(
                update={
                    "repositories": [
                        repository.model_copy(
                            update={
                                "declared_surface": next(
                                    (
                                        inventory.declared_surface
                                        for inventory in inventories
                                        if inventory.repo_id == repository.repo_id
                                    ),
                                    repository.declared_surface,
                                )
                            }
                        )
                        for repository in workspace.repositories
                    ]
                }
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
        if repository_surfaces:
            selected_surfaces = sorted(
                {
                    surface
                    for inventory in inventories
                    for surface in [inventory.detected_surface or inventory.declared_surface]
                    if surface is not None
                }
            )
            fields["evidence_surfaces"] = fields["evidence_surfaces"].model_copy(
                update={
                    "value": selected_surfaces,
                    "source": "human_confirmed",
                    "confidence": "high",
                }
            )
            fields["repository_roots"] = fields["repository_roots"].model_copy(
                update={
                    "value": {
                        surface: [
                            inventory.path
                            for inventory in inventories
                            if surface in [inventory.detected_surface or inventory.declared_surface]
                        ]
                        for surface in selected_surfaces
                    },
                    "source": "human_confirmed",
                    "confidence": "high",
                }
            )
        confirmed = profile.model_copy(update={"status": "confirmed", "fields": fields})
        validation = ProfileValidator().validate(confirmed, inventories, app_facts)
        self.store.write_workspace(workspace)
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
        human_answers: Optional[dict[str, object]] = None,
    ) -> ReviewSetupResult:
        """Compile confirmed setup state into Coverage Units and Runtime Work Items."""
        workspace = workspace or self._load_workspace()
        if not workspace.repositories:
            raise ReviewSetupError("at least one workspace repository is required")
        if not 1 <= max_concurrency <= 32:
            raise ReviewSetupError("max_concurrency must be between 1 and 32")

        inventories = [build_repository_inventory(repo) for repo in workspace.repositories]
        app_facts = collect_app_facts(inventories, workspace.materials)
        profile = self._load_confirmed_profile()
        profile_validation = ProfileValidator().validate(profile, inventories, app_facts)
        if not profile_validation.valid:
            raise ReviewSetupError(
                "confirmed AppProfile failed deterministic validation: "
                + "; ".join([*profile_validation.errors, *profile_validation.conflicts])
            )

        controls, control_validation = self._load_validated_controls()
        source_registry, obligations = self._load_policy_artifacts()
        applicability_profile = _to_applicability_profile(profile, inventories, workspace.materials)
        applicability_loop = ApplicabilityResolutionLoop(
            self.applicability_provider,
            source_registry=source_registry,
            obligations=obligations,
            max_concurrency=min(
                max_concurrency, ApplicabilityResolutionLoop.MAX_CONCURRENCY
            ),
        )
        profile_fingerprint = _fingerprint(profile.model_dump(mode="json"))
        control_fingerprint = _fingerprint(controls.model_dump(mode="json"))
        stored_answers = self._load_applicability_answers(
            profile_fingerprint, control_fingerprint
        )
        effective_answers = {**stored_answers, **(human_answers or {})}
        if human_answers:
            self.store.write_applicability_answers(
                ApplicabilityAnswerSet(
                    profile_fingerprint=profile_fingerprint,
                    control_fingerprint=control_fingerprint,
                    answers=effective_answers,
                )
            )
        resolution_profile_fingerprint = _fingerprint(
            {
                "profile": applicability_profile.model_dump(mode="json"),
                "answers": effective_answers,
            }
        )
        checkpoint = self._load_applicability_checkpoint(
            resolution_profile_fingerprint, control_fingerprint
        )

        def write_checkpoint(
            decisions: list[ApplicabilityDecision], attempts: int, tool_calls: int
        ) -> None:
            self.store.write_applicability_checkpoint(
                ApplicabilityResolutionCheckpoint(
                    profile_fingerprint=resolution_profile_fingerprint,
                    control_fingerprint=control_fingerprint,
                    decisions=decisions,
                    attempts=attempts,
                    tool_calls=tool_calls,
                )
            )

        effective_profile, applicability, applicability_resolution = applicability_loop.resolve(
            applicability_profile,
            controls,
            inventories,
            app_facts,
            human_answers=effective_answers,
            initial_decisions=checkpoint.decisions if checkpoint else (),
            checkpoint_callback=write_checkpoint,
            initial_attempts=checkpoint.attempts if checkpoint else 0,
            initial_tool_calls=checkpoint.tool_calls if checkpoint else 0,
        )
        available_surfaces = {
            surface
            for inventory in inventories
            for surface in [(inventory.detected_surface or inventory.declared_surface)]
            if surface is not None
        }
        available_surfaces.update(
            material.surface
            for material in workspace.materials
            if material.surface is not None and Path(material.path).expanduser().exists()
        )
        coverage = CoverageUnitBuilder().build(
            effective_profile,
            controls,
            applicability,
            available_surfaces=available_surfaces,
        )
        selected_run_id = run_id or _new_run_id()
        run_paths = self.store.prepare_run_artifacts(selected_run_id)
        plan = WorkItemPlanner().plan(
            effective_profile,
            controls,
            coverage,
            app_facts,
            inventories,
            run_paths["run_root"],
            materials=workspace.materials,
            external_evidence_policy=workspace.external_evidence_policy,
        )
        manifest = ReviewManifest(
            contract="review_manifest.v2",
            run_id=selected_run_id,
            mode=mode,
            default_max_concurrency=max_concurrency,
            surface_roots={
                work_item.work_item_id: plan.sandboxes[work_item.work_item_id].root.as_posix()
                for work_item in plan.work_items
            },
            work_items=plan.work_items,
            excluded_controls=[
                ExcludedControl(
                    control_id=control_id,
                    reason="validated semantic applicability decision is not_applicable",
                )
                for control_id in plan.coverage.excluded_control_ids
            ],
            coverage_unit_ids=[unit.coverage_unit_id for unit in plan.coverage.units],
            unknown_control_ids=plan.coverage.unknown_control_ids,
            missing_surfaces=plan.coverage.missing_surfaces,
            source_profile_version=effective_profile.version,
            source_control_version=controls.version,
        )
        self.store.write_workspace(workspace)
        self.store.write_repository_inventory(inventories)
        self.store.write_app_facts(app_facts)
        self.store.write_applicability(applicability)
        self.store.write_applicability_profile(effective_profile)
        self.store.write_coverage_units(plan.coverage)
        self.store.write_applicability_resolution(applicability_resolution)
        self.store.remove_legacy_applicability_discovery_artifacts()
        self.store.write_review_manifest(selected_run_id, manifest)
        return ReviewSetupResult(
            workspace=workspace,
            inventories=inventories,
            app_facts=app_facts,
            profile=profile,
            profile_validation=profile_validation,
            confirmation=ProfileConfirmation(
                status=(
                    "deferred_to_applicability"
                    if applicability_resolution.pending_questions
                    else "confirmed"
                ),
                required_fields=[
                    question.fact_key for question in applicability_resolution.pending_questions
                ],
                conflicts=[],
                confirmed_fields=[],
            ),
            applicability_profile=effective_profile,
            applicability=applicability,
            coverage=plan.coverage,
            manifest=manifest,
            run_id=selected_run_id,
            work_items=plan.work_items,
            sandboxes=plan.sandboxes,
            collector_results=plan.collector_results,
            applicability_resolution=applicability_resolution,
        )

    def compile_diff_from_baseline(
        self,
        semantic_baseline_run_id: str,
        *,
        run_id: Optional[str] = None,
        max_concurrency: int = 3,
    ) -> ReviewSetupResult:
        """Rebuild only code/runtime inputs while preserving frozen review semantics."""
        try:
            frozen = self.store.read_run_json(semantic_baseline_run_id, "semantic-setup.json")
            workspace = ComplianceWorkspace.model_validate(frozen["workspace"])
            profile = AppProfile.model_validate(frozen["profile"])
            applicability_profile = ApplicabilityProfile.model_validate(
                frozen["applicability_profile"]
            )
            applicability = ApplicabilitySet.model_validate(frozen["applicability"])
            coverage = CoverageSet.model_validate(frozen["coverage"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ReviewSetupError(
                "baseline lacks frozen semantic setup; run a new Full Review"
            ) from exc
        baseline_inventories = [
            RepositoryInventory.model_validate(item) for item in frozen.get("inventories", [])
        ]
        inventories = [build_repository_inventory(repo) for repo in workspace.repositories]
        if _inventory_mapping(baseline_inventories) != _inventory_mapping(inventories):
            raise ReviewSetupError("repository or surface mapping changed; run a Full Review")
        try:
            controls = ControlSet.model_validate(frozen["controls"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewSetupError(
                "baseline lacks frozen Control Set; run a new Full Review"
            ) from exc
        if controls.version != frozen.get("control_version"):
            raise ReviewSetupError("frozen Control Set version is invalid; run a Full Review")
        app_facts = collect_app_facts(inventories, workspace.materials)
        selected_run_id = run_id or _new_run_id()
        run_paths = self.store.prepare_run_artifacts(selected_run_id)
        plan = WorkItemPlanner().plan(
            applicability_profile,
            controls,
            coverage,
            app_facts,
            inventories,
            run_paths["run_root"],
            materials=workspace.materials,
            external_evidence_policy=workspace.external_evidence_policy,
        )
        manifest = ReviewManifest(
            contract="review_manifest.v2",
            run_id=selected_run_id,
            mode="diff",
            default_max_concurrency=max_concurrency,
            surface_roots={
                item.work_item_id: plan.sandboxes[item.work_item_id].root.as_posix()
                for item in plan.work_items
            },
            work_items=plan.work_items,
            coverage_unit_ids=[unit.coverage_unit_id for unit in coverage.units],
            excluded_controls=[],
            unknown_control_ids=coverage.unknown_control_ids,
            missing_surfaces=coverage.missing_surfaces,
            source_profile_version=applicability_profile.version,
            source_control_version=controls.version,
        )
        self.store.write_review_manifest(selected_run_id, manifest)
        return ReviewSetupResult(
            workspace=workspace,
            inventories=inventories,
            app_facts=app_facts,
            profile=profile,
            profile_validation=ProfileValidationResult(valid=True),
            confirmation=ProfileConfirmation(status="confirmed"),
            applicability_profile=applicability_profile,
            applicability=applicability,
            coverage=coverage,
            manifest=manifest,
            run_id=selected_run_id,
            work_items=plan.work_items,
            sandboxes=plan.sandboxes,
            collector_results=plan.collector_results,
        )

    def _load_workspace(self) -> ComplianceWorkspace:
        path = self.workspace_root / "workspace.json"
        if not path.is_file():
            raise ReviewSetupError(f"workspace.json does not exist: {path}")
        try:
            return ComplianceWorkspace.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(f"invalid workspace.json: {exc}") from exc

    def _load_confirmed_profile(self) -> AppProfile:
        path = self.workspace_root / "setup" / "app_profile.json"
        if not path.is_file():
            path = self.workspace_root / "setup" / "app_profile_draft.json"
            if not path.is_file():
                raise ReviewSetupError(
                    "AppProfile is missing at setup/app_profile.json or "
                    "setup/app_profile_draft.json"
                )
        try:
            return AppProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(f"invalid confirmed AppProfile: {exc}") from exc

    def _load_applicability_answers(
        self, profile_fingerprint: str, control_fingerprint: str
    ) -> dict[str, object]:
        path = self.workspace_root / "setup" / "applicability_answers.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewSetupError(f"invalid applicability_answers.json: {exc}") from exc
        try:
            answer_set = ApplicabilityAnswerSet.model_validate(value)
        except ValueError:
            # Plain dictionaries are the pre-versioned format. They cannot be
            # safely associated with the current profile/control snapshot.
            return {}
        if (
            answer_set.profile_fingerprint != profile_fingerprint
            or answer_set.control_fingerprint != control_fingerprint
        ):
            return {}
        return answer_set.answers

    def _load_applicability_checkpoint(
        self, profile_fingerprint: str, control_fingerprint: str
    ) -> ApplicabilityResolutionCheckpoint | None:
        path = self.workspace_root / "setup" / "applicability_resolution_checkpoint.json"
        if not path.is_file():
            return None
        try:
            checkpoint = ApplicabilityResolutionCheckpoint.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(
                f"invalid applicability_resolution_checkpoint.json: {exc}"
            ) from exc
        if (
            checkpoint.profile_fingerprint != profile_fingerprint
            or checkpoint.control_fingerprint != control_fingerprint
        ):
            return None
        return checkpoint

    def _load_validated_controls(self) -> tuple[ControlSet, ControlValidationResult]:
        controls_path = self.workspace_root / "setup" / "controls.json"
        validation_path = self.workspace_root / "setup" / "control_validation.json"
        if not controls_path.is_file() or not validation_path.is_file():
            raise ReviewSetupError(
                "validated controls and control_validation.json are required before review setup"
            )
        try:
            controls = adapt_control_set(json.loads(controls_path.read_text(encoding="utf-8")))
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
            raise ReviewSetupError("control_validation count does not match setup/controls.json")
        return controls, validation

    def _load_policy_artifacts(
        self,
    ) -> tuple[SourceRegistry | None, list[Obligation]]:
        sources_path = self.workspace_root / "setup" / "sources.json"
        obligations_path = self.workspace_root / "setup" / "obligations.json"
        if not sources_path.is_file() and not obligations_path.is_file():
            return None, []
        if not sources_path.is_file() or not obligations_path.is_file():
            raise ReviewSetupError(
                "setup/sources.json and setup/obligations.json must be provided together"
            )
        try:
            source_registry = (
                SourceRegistry.model_validate(json.loads(sources_path.read_text(encoding="utf-8")))
            )
            obligation_set = (
                ObligationSet.model_validate(
                    json.loads(obligations_path.read_text(encoding="utf-8"))
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewSetupError(f"invalid persisted policy artifacts: {exc}") from exc
        # Phase 2 keeps the obligation artifact as ``draft`` while its compiled
        # Control Set is validated. Applicability needs the persisted policy
        # semantics and provenance, not a second obligation approval state.
        return source_registry, obligation_set.obligations

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


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _inventory_mapping(
    inventories: Sequence[RepositoryInventory],
) -> dict[str, tuple[str, Surface | None]]:
    """Compare only stable repository/surface topology, never mutable Git state."""
    return {
        item.repo_id: (
            Path(item.path).expanduser().resolve().as_posix(),
            item.detected_surface or item.declared_surface,
        )
        for item in inventories
    }


def _to_applicability_profile(
    profile: AppProfile,
    inventories: Sequence[RepositoryInventory],
    materials: Sequence[WorkspaceMaterial] = (),
) -> ApplicabilityProfile:
    def optional_value(name: str, default: object = "unknown") -> object:
        field = profile.fields.get(name)
        return default if field is None or field.value is None else field.value

    raw_business_type = optional_value("business_type", ["unknown"])
    business_type = (
        [raw_business_type]
        if isinstance(raw_business_type, str)
        else list(raw_business_type)
        if isinstance(raw_business_type, list)
        else None
    )
    if not business_type or not all(isinstance(item, str) and item for item in business_type):
        business_type = ["unknown"]
    raw_surfaces = optional_value("evidence_surfaces", [])
    if not isinstance(raw_surfaces, list) or not raw_surfaces:
        raw_surfaces = [
            inventory.detected_surface or inventory.declared_surface
            for inventory in inventories
            if inventory.detected_surface or inventory.declared_surface
        ]
    material_surfaces = [material.surface for material in materials if material.surface is not None]
    if isinstance(raw_surfaces, list):
        raw_surfaces = list(dict.fromkeys([*raw_surfaces, *material_surfaces]))
    if not raw_surfaces:
        raise ReviewSetupError("at least one repository evidence surface is required")
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
            if isinstance(raw_root, list) and raw_root:
                roots[surface] = str(raw_root[0])
            elif isinstance(raw_surface, str) and isinstance(raw_root, str):
                roots[surface] = raw_root
    for inventory in inventories:
        inventory_surface = inventory.detected_surface or inventory.declared_surface
        if inventory_surface is not None:
            roots.setdefault(inventory_surface, inventory.path)
    for material in materials:
        if material.surface is not None:
            roots.setdefault(material.surface, material.path)
    raw_self_lending = optional_value("self_lending", "unknown")
    if isinstance(raw_self_lending, bool):
        self_lending: bool | Literal["unknown"] = raw_self_lending
    elif raw_self_lending == "unknown":
        self_lending = "unknown"
    else:
        raise ReviewSetupError("AppProfile self_lending must be true, false, or unknown")
    review_scope: Literal["full_release_package", "multi_surface_static_review", "partial"] = (
        profile.value_for("review_scope", "partial")
    )
    if review_scope not in {"full_release_package", "multi_surface_static_review", "partial"}:
        review_scope = "partial"
    try:
        confirmed_facts = {
            name: ApplicabilityProfileFact(value=field.value, source=field.source)
            for name, field in profile.fields.items()
        }
        return ApplicabilityProfile(
            contract="applicability_profile.v2",
            version="2.0",
            app_name=str(optional_value("app_name")),
            package_name=str(optional_value("package_name")),
            jurisdiction=str(optional_value("jurisdiction")),
            business_type=business_type,
            self_lending=self_lending,
            evidence_surfaces=surfaces,
            review_scope=review_scope,
            roots=roots,
            confirmed_facts=confirmed_facts,
        )
    except ValueError as exc:
        raise ReviewSetupError(
            "confirmed AppProfile cannot become applicability profile: " + str(exc)
        ) from exc
