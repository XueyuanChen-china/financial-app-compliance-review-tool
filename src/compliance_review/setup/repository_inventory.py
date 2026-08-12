from __future__ import annotations

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
    files = set(sandbox.list_files("**/*", limit=5000))
    signals: list[DetectionSignal] = []
    detected: list[Surface] = []

    def add(surface: Surface, signal_type: str, path: str, detail: str) -> None:
        if surface not in detected:
            detected.append(surface)
        signals.append(
            DetectionSignal(signal_type=signal_type, path=path, detail=detail)
        )

    has_android_manifest = "AndroidManifest.xml" in files or any(
        path.endswith("/AndroidManifest.xml") for path in files
    )
    if has_android_manifest:
        add("android_native", "android_manifest", "AndroidManifest.xml", "Android manifest found")
    has_gradle = any(path.endswith(("build.gradle", "build.gradle.kts")) for path in files)
    if has_gradle and has_android_manifest:
        add("android_native", "gradle_build", "build.gradle", "Gradle build file found")
    if "package.json" in files:
        add("frontend_h5", "package_manifest", "package.json", "Node package manifest found")
        package_text = sandbox.read_text("package.json")
        for framework in ("react", "vue", "@angular/core", "svelte"):
            if framework in package_text.lower():
                signals.append(
                    DetectionSignal(
                        signal_type="frontend_framework",
                        path="package.json",
                        detail=f"frontend framework dependency found: {framework}",
                    )
                )
                break
    if any(path.endswith("pom.xml") for path in files) or (has_gradle and not has_android_manifest):
        add("backend_code", "backend_build_or_layout", "", "backend build/layout signal found")
    if any(
        path.lower().endswith((".openapi.json", ".openapi.yaml", ".openapi.yml"))
        or path.lower().endswith(("swagger.json", "swagger.yaml", "swagger.yml"))
        for path in files
    ):
        api_path = next(
            path
            for path in sorted(files)
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
