from pathlib import Path
from typing import Annotated, Optional

import typer

from compliance_review import __version__
from compliance_review.code_map import CodeMapQuery, GraphifyCodeMapProvider
from compliance_review.code_map.provider import command_from_string
from compliance_review.config.loader import ConfigLoadError, load_controls, load_profile

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
