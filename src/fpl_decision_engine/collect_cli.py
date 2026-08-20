"""CLI wiring for the Premier League team-news collection workflow."""

from pathlib import Path
from typing import Annotated

import typer

from fpl_decision_engine.infrastructure.providers.team_news import (
    PremierLeagueInjuriesCollector,
)
from fpl_decision_engine.ports import ProviderError


def collect_command(
    bootstrap: Annotated[Path, typer.Option(help="Contemporaneous FPL bootstrap-static.json.")],
    output: Annotated[Path, typer.Option(help="Capture output root directory.")],
) -> None:
    """Collect the Premier League latest-player-injuries page into immutable evidence."""

    collector = PremierLeagueInjuriesCollector(output_root=output, bootstrap_path=bootstrap)
    try:
        capture = collector.collect()
    except ProviderError as exc:
        typer.echo(
            f"error [{exc.code}] provider={exc.provider_id}: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"capture_id: {capture.capture_id}")
    typer.echo(f"capture_directory: {capture.path}")
    typer.echo(f"structured_evidence: {capture.structured_evidence_path}")
    typer.echo(f"evaluation: {capture.path / 'evaluation.json'}")
