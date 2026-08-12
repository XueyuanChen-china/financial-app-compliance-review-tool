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
