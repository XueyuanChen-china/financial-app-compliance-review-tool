from __future__ import annotations

import json
from pathlib import Path

from compliance_review.domain.models import Surface
from compliance_review.repository import GitRepository, RepositorySandbox
from compliance_review.setup.models import (
    DetectionSignal,
    RepositoryInventory,
    SurfaceStatus,
    WorkspaceRepository,
)


def build_repository_inventory(
    repository: WorkspaceRepository,
) -> RepositoryInventory:
    root = Path(repository.path).expanduser().resolve()
    sandbox = RepositorySandbox(root)
    manifest_paths = sandbox.list_files("**/AndroidManifest.xml", limit=100)
    package_paths = sandbox.list_files("**/package.json", limit=100)
    gradle_paths = sandbox.list_files("**/build.gradle*", limit=100)
    pom_paths = sandbox.list_files("**/pom.xml", limit=100)
    api_paths = sorted(
        {
            path
            for pattern in (
                "**/*.openapi.json",
                "**/*.openapi.yaml",
                "**/*.openapi.yml",
                "**/swagger.json",
                "**/swagger.yaml",
                "**/swagger.yml",
            )
            for path in sandbox.list_files(pattern, limit=100)
        }
    )
    signals: list[DetectionSignal] = []
    detected: list[Surface] = []

    def add(surface: Surface, signal_type: str, path: str, detail: str) -> None:
        if surface not in detected:
            detected.append(surface)
        signals.append(
            DetectionSignal(signal_type=signal_type, path=path, detail=detail)
        )

    has_android_manifest = bool(manifest_paths)
    if has_android_manifest:
        add(
            "android_native",
            "android_manifest",
            manifest_paths[0],
            "Android manifest found",
        )
    has_gradle = bool(gradle_paths)
    if has_gradle and has_android_manifest:
        add("android_native", "gradle_build", gradle_paths[0], "Gradle build file found")
    for package_path in package_paths:
        try:
            package = json.loads(sandbox.read_text(package_path))
        except (OSError, ValueError, TypeError):
            continue
        dependencies = {
            name.lower()
            for group in ("dependencies", "devDependencies", "peerDependencies")
            for name in (package.get(group, {}) if isinstance(package.get(group, {}), dict) else {})
        }
        for framework in ("react", "vue", "@angular/core", "svelte"):
            if framework.lower() in dependencies:
                add(
                    "frontend_h5",
                    "package_manifest",
                    package_path,
                    "Frontend package manifest found",
                )
                signals.append(
                    DetectionSignal(
                        signal_type="frontend_framework",
                        path=package_path,
                        detail=f"frontend framework dependency found: {framework}",
                    )
                )
                break
    if pom_paths or (has_gradle and not has_android_manifest):
        add(
            "backend_code",
            "backend_build_or_layout",
            (pom_paths or gradle_paths)[0],
            "backend build/layout signal found",
        )
    if any(
        path.lower().endswith((".openapi.json", ".openapi.yaml", ".openapi.yml"))
        or path.lower().endswith(("swagger.json", "swagger.yaml", "swagger.yml"))
        for path in api_paths
    ):
        api_path = next(
            path
            for path in sorted(api_paths)
            if path.lower().endswith(
                (
                    ".openapi.json",
                    ".openapi.yaml",
                    ".openapi.yml",
                    "swagger.json",
                    "swagger.yaml",
                    "swagger.yml",
                )
            )
        )
        add("backend_api_doc", "api_document", api_path, "OpenAPI/Swagger document found")

    declared = repository.declared_surface
    if declared is not None:
        status: SurfaceStatus = "confirmed" if declared in detected else "unresolved"
        detected_surface = declared if status == "confirmed" else None
    elif len(detected) == 1:
        status = "confirmed"
        detected_surface = detected[0]
    else:
        status = "unresolved"
        detected_surface = None

    git = GitRepository(root).metadata()
    return RepositoryInventory(
        repo_id=repository.repo_id,
        path=root.as_posix(),
        declared_surface=declared,
        detected_surface=detected_surface,
        detected_surfaces=detected,
        surface_status=status,
        detection_signals=signals,
        git_revision=git.revision,
        is_git_repository=git.is_git_repository,
        is_dirty=git.is_dirty,
        changed_files=list(git.changed_files),
        git_error_code=git.error_code,
    )
