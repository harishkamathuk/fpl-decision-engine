"""Command-line entry point for the FPL Decision Engine."""

import typer

from fpl_decision_engine import __version__

app = typer.Typer(no_args_is_help=True, help="FPL Decision Engine")


@app.command()
def version() -> None:
    """Show the installed application version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
