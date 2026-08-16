"""CLI wiring for the offline snapshot sync workflow."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from fpl_decision_engine.application.sync_data import sync_data
from fpl_decision_engine.infrastructure.ingestion import SnapshotStore, prepare_snapshot
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import (
    FplSnapshotProvider,
    map_snapshot,
)
from fpl_decision_engine.ports import ProviderError


def sync_command(
    source: Annotated[str, typer.Option(help="Input mode; Issue #3 supports 'snapshot'.")],
    input_path: Annotated[Path, typer.Option("--input", help="Snapshot directory or manifest.")],
) -> None:
    """Import and canonicalise an immutable offline source snapshot."""

    if source != "snapshot":
        raise typer.BadParameter("only the offline 'snapshot' source is currently supported")

    now = datetime.now(UTC)
    try:
        prepared = prepare_snapshot(input_path)
        canonical = map_snapshot(prepared)
        prepared = prepared.with_season(canonical.season)
        stored = SnapshotStore(Path("data/raw")).store(prepared, imported_at=now)
        provider = FplSnapshotProvider(
            canonical,
            provider_id=stored.manifest.provider_id,
            snapshot_id=stored.manifest.snapshot_id,
            observed_at=stored.manifest.observed_at,
            imported_at=stored.manifest.imported_at,
            source_reference=str(stored.path),
        )
        result = sync_data(
            provider,
            now=now,
            evidence_location=stored.path,
            warnings=provider.warnings,
            created=stored.created,
        )
    except ProviderError as exc:
        typer.echo(
            f"error [{exc.code}] provider={exc.provider_id}: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"snapshot_id: {result.snapshot_id}")
    typer.echo(f"provider_id: {result.provider_id}")
    typer.echo(f"observed_at: {result.observed_at.isoformat()}")
    typer.echo(f"age_seconds: {int(result.age.total_seconds())}")
    typer.echo(
        "mapped: "
        f"gameweeks={result.gameweek_count} "
        f"teams={result.team_count} "
        f"players={result.player_count} "
        f"fixtures={result.fixture_count}"
    )
    typer.echo(f"warnings: {len(result.warnings)}")
    typer.echo(f"evidence: {result.evidence_location}")
    typer.echo(f"import: {'created' if result.created else 'existing-identical'}")
