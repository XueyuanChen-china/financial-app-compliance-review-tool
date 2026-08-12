import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import TypeAdapter, ValidationError

from compliance_review import __version__
from compliance_review.code_map import CodeMapQuery, GraphifyCodeMapProvider
from compliance_review.code_map.provider import command_from_string
from compliance_review.collectors import DependencyCollector, ManifestCollector, RouteApiCollector
from compliance_review.config.loader import ConfigLoadError, load_controls, load_profile
from compliance_review.domain.models import Surface
from compliance_review.repository import GitRepository, ReadOnlyRepositoryTools, RepositorySandbox
from compliance_review.review import (
    OpenAICompatibleProvider,
    ReviewManifestBuilder,
    ReviewScheduler,
)
from compliance_review.review.models import ReviewManifest

app = typer.Typer(
    name="compliance-review",
    help="Evidence-aware compliance review for financial applications.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed project version."""
    typer.echo(__version__)


@app.command()
def review(
    target: Annotated[str, typer.Argument(help="Repository or release package to review")],
    mode: Annotated[
        str,
        typer.Option(help="Review mode: full or diff"),
    ] = "full",
) -> None:
    """Reserve the public review entry point while the engine is implemented."""
    raise typer.BadParameter(
        f"Review engine is not implemented yet (target={target!r}, mode={mode!r})."
    )


@app.command()
def validate(
    profile: Annotated[Path, typer.Option(help="Applicability profile YAML")],
    controls: Annotated[Path, typer.Option(help="Control set YAML")],
) -> None:
    """Validate the Day 1 profile and control contracts."""
    try:
        loaded_profile = load_profile(profile)
        loaded_controls = load_controls(controls)
    except ConfigLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(
        f"valid: profile={loaded_profile.app_name} "
        f"controls={len(loaded_controls.controls)}"
    )


@app.command("code-map-query")
def code_map_query(
    repo: Annotated[Path, typer.Option(help="Repository to query")],
    query: Annotated[str, typer.Option(help="Business or Control-oriented query")],
    surface: Annotated[Optional[str], typer.Option(help="Optional evidence surface")] = None,
    graphify_command: Annotated[
        str, typer.Option(help="Graphify executable command; no shell is used")
    ] = "graphify",
) -> None:
    """Query Graphify through the stable local Code Map provider boundary."""
    try:
        command = command_from_string(graphify_command)
        request = CodeMapQuery.model_validate({"query": query, "surface": surface})
        result = GraphifyCodeMapProvider(repo, command=command).query(request)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(result.model_dump_json(indent=2))


@app.command("repository-info")
def repository_info(
    repo: Annotated[Path, typer.Option(help="Repository to inspect")],
) -> None:
    """Print bounded, read-only repository and Git metadata."""
    try:
        sandbox = RepositorySandbox(repo)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    metadata = GitRepository(sandbox.root).metadata()
    typer.echo(json.dumps(metadata.__dict__, indent=2, sort_keys=True))


@app.command("search-code")
def search_code(
    repo: Annotated[Path, typer.Option(help="Repository to search")],
    query: Annotated[str, typer.Option(help="Exact text to search")],
    root: Annotated[str, typer.Option(help="Relative search root")] = ".",
    limit: Annotated[int, typer.Option(help="Maximum matches")] = 100,
) -> None:
    """Run a bounded read-only search with Git grep fallback."""
    try:
        tools = ReadOnlyRepositoryTools(RepositorySandbox(repo))
        matches = tools.search_code(query, roots=(root,), limit=limit)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("\n".join(str(match.__dict__) for match in matches))


@app.command("collect")
def collect(
    repo: Annotated[Path, typer.Option(help="Repository to collect facts from")],
    collector: Annotated[str, typer.Option(help="manifest, dependencies, or routes")],
    surface: Annotated[Optional[str], typer.Option(help="Evidence surface override")] = None,
    root: Annotated[str, typer.Option(help="Relative route/API scan root")] = "src",
) -> None:
    """Run one deterministic Collector and print its structured result."""
    try:
        sandbox = RepositorySandbox(repo)
        parsed_surface = (
            TypeAdapter(Surface).validate_python(surface) if surface else None
        )
        if collector == "manifest":
            result = ManifestCollector().collect(sandbox)
        elif collector == "dependencies":
            result = DependencyCollector().collect(
                sandbox,
                source_surface=parsed_surface or "android_native",
            )
        elif collector == "routes":
            result = RouteApiCollector().collect(
                sandbox,
                roots=(root,),
                source_surface=parsed_surface or "frontend_h5",
            )
        else:
            raise ValueError("collector must be manifest, dependencies, or routes")
    except (OSError, ValueError, ValidationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(result.model_dump_json(indent=2))


@app.command("build-manifest")
def build_manifest(
    profile: Annotated[Path, typer.Option(help="Applicability profile YAML")],
    controls: Annotated[Path, typer.Option(help="Control set YAML")],
    run_id: Annotated[str, typer.Option(help="Stable review run identifier")],
    output: Annotated[Path, typer.Option(help="Manifest JSON output path")],
    max_concurrency: Annotated[int, typer.Option(help="Default worker concurrency")] = 3,
) -> None:
    """Build a deterministic module-by-surface Review Manifest."""
    try:
        loaded_profile = load_profile(profile)
        loaded_controls = load_controls(controls)
        manifest = ReviewManifestBuilder().build(
            loaded_profile,
            loaded_controls,
            run_id=run_id,
            max_concurrency=max_concurrency,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    except (ConfigLoadError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(output.as_posix())


@app.command("run-review")
def run_review(
    manifest: Annotated[Path, typer.Option(help="Review Manifest JSON")],
    output_root: Annotated[Path, typer.Option(help="Per-work-item output directory")],
    model: Annotated[str, typer.Option(help="OpenAI-compatible model name")],
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible chat completions URL")
    ] = "https://api.openai.com/v1/chat/completions",
    max_concurrency: Annotated[int, typer.Option(help="Maximum parallel workers")] = 3,
) -> None:
    """Run a manifest using an OpenAI-compatible provider and read-only tools."""
    try:
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        review_manifest = ReviewManifest.model_validate(loaded)
        sandboxes = {
            surface: RepositorySandbox(Path(root))
            for surface, root in review_manifest.surface_roots.items()
        }
        summary = ReviewScheduler(
            provider=OpenAICompatibleProvider(model=model, base_url=base_url),
            max_concurrency=max_concurrency,
        ).run(
            manifest_run_id=review_manifest.run_id,
            work_items=review_manifest.work_items,
            sandboxes=sandboxes,
            output_root=output_root,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(summary.model_dump_json(indent=2))
