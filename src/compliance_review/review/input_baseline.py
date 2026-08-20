from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, cast

from compliance_review.domain.models import ReviewInputBaseline, ReviewInputFingerprint
from compliance_review.setup.models import ComplianceWorkspace

_SETUP_INPUTS: tuple[tuple[str, str, str], ...] = (
    ("sources", "sources", "setup/sources.json"),
    ("obligations", "obligations", "setup/obligations.json"),
    ("controls", "controls", "setup/controls.json"),
    ("app_profile", "app_profile", "setup/app_profile.json"),
    ("inventory", "inventory", "setup/repository_inventory.json"),
    ("applicability", "applicability", "setup/applicability.json"),
    ("workspace", "workspace", "workspace.json"),
)


def collect_review_input_baseline(workspace_root: Path, run_id: str) -> ReviewInputBaseline:
    """Fingerprint every non-code input that can change review semantics."""
    root = workspace_root.expanduser().resolve()
    artifacts: list[ReviewInputFingerprint] = []
    for artifact_id, category, relative_path in _SETUP_INPUTS:
        target = root / relative_path
        if target.exists():
            artifacts.append(
                ReviewInputFingerprint(
                    artifact_id=artifact_id,
                    category=cast(
                        Literal[
                            "controls",
                            "obligations",
                            "sources",
                            "app_profile",
                            "api_documents",
                            "play_console",
                            "regulator_external",
                            "other_external",
                            "inventory",
                            "applicability",
                            "workspace",
                        ],
                        category,
                    ),
                    path=relative_path,
                    sha256=_path_hash(target),
                )
            )
    workspace_path = root / "workspace.json"
    if workspace_path.is_file():
        workspace = ComplianceWorkspace.model_validate_json(
            workspace_path.read_text(encoding="utf-8")
        )
        for index, material in enumerate(workspace.materials):
            path = Path(material.path).expanduser()
            if not path.exists():
                continue
            category = _material_category(material.surface)
            artifacts.append(
                ReviewInputFingerprint(
                    artifact_id=f"material.{index}",
                    category=cast(
                        Literal[
                            "controls",
                            "obligations",
                            "sources",
                            "app_profile",
                            "api_documents",
                            "play_console",
                            "regulator_external",
                            "other_external",
                            "inventory",
                            "applicability",
                            "workspace",
                        ],
                        category,
                    ),
                    path=path.resolve().as_posix(),
                    sha256=_path_hash(path),
                )
            )
    return ReviewInputBaseline(run_id=run_id, artifacts=artifacts)


def _material_category(surface: str | None) -> str:
    if surface == "backend_api_doc":
        return "api_documents"
    if surface == "play_console":
        return "play_console"
    if surface == "regulator_external":
        return "regulator_external"
    return "other_external"


def _path_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(child.read_bytes())
    return digest.hexdigest()


def load_input_baseline(workspace_root: Path, run_id: str) -> ReviewInputBaseline:
    path = workspace_root.expanduser().resolve() / "runs" / run_id / "review-input-baseline.json"
    return ReviewInputBaseline.model_validate_json(path.read_text(encoding="utf-8"))


def render_preflight_json(baseline: ReviewInputBaseline, current: ReviewInputBaseline) -> str:
    """Stable artifact for callers that need a machine-readable preflight result."""
    return json.dumps(
        baseline.compare(current.artifacts).model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
