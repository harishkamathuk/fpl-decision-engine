"""Command-line entry point for the FPL Decision Engine."""

import typer

from fpl_decision_engine import __version__
from fpl_decision_engine.collect_cli import collect_command
from fpl_decision_engine.sync_cli import sync_command

app = typer.Typer(no_args_is_help=True, help="FPL Decision Engine")
app.command(name="sync")(sync_command)
app.command(name="collect")(collect_command)


@app.command()
def version() -> None:
    """Show the installed application version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
