from typing import Annotated

import typer

from compliance_review import __version__

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
