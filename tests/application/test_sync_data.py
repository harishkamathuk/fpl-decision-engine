from datetime import UTC, datetime
from pathlib import Path

from fpl_decision_engine.application.sync_data import sync_data
from fpl_decision_engine.infrastructure.ingestion import SnapshotStore, prepare_snapshot
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import (
    FplSnapshotProvider,
    map_snapshot,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "fpl_snapshot"
NOW = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)


def test_sync_data_reports_canonical_counts_and_provenance(tmp_path: Path) -> None:
    prepared = prepare_snapshot(FIXTURE_ROOT)
    canonical = map_snapshot(prepared)
    stored = SnapshotStore(tmp_path).store(prepared.with_season(canonical.season), imported_at=NOW)
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
        now=NOW,
        evidence_location=stored.path,
        warnings=provider.warnings,
        created=stored.created,
    )

    assert result.snapshot_id == stored.manifest.snapshot_id
    assert result.provider_id == "synthetic-fpl"
    assert result.gameweek_count == 1
    assert result.team_count == 2
    assert result.player_count == 2
    assert result.fixture_count == 1
    assert len(result.warnings) == 1
