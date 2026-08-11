from pathlib import Path
from typing import Annotated

import typer

from compliance_review import __version__
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
