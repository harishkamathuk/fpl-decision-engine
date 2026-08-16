from __future__ import annotations

import socket
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import polars as pl
import pytest

from fpl_decision_engine.application.persist_data import persist_provider_snapshot
from fpl_decision_engine.domain import (
    DecisionRun,
    DecisionRunStatus,
    Fixture,
    GameweekNumber,
    Money,
    Player,
    Position,
)
from fpl_decision_engine.infrastructure.ingestion import SnapshotStore, prepare_snapshot
from fpl_decision_engine.infrastructure.persistence import (
    DuckDbDecisionRunRepository,
    DuckDbSnapshotCatalog,
    ParquetCanonicalRepository,
)
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import (
    FplSnapshotProvider,
    map_snapshot,
)
from fpl_decision_engine.ports import (
    CanonicalDatasetName,
    CanonicalSnapshot,
    ImmutableRegistrationConflict,
    PersistenceError,
    SnapshotRegistration,
    SourceObjectHash,
    UnsupportedSchemaVersion,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "fpl_snapshot"
OBSERVED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
PROCESSED_AT = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def registration(snapshot_id: str = "snapshot-a") -> SnapshotRegistration:
    return SnapshotRegistration(
        provider_id="synthetic-fpl",
        season="2026-27",
        snapshot_id=snapshot_id,
        observed_at=OBSERVED_AT,
        processed_at=PROCESSED_AT,
        schema_version=1,
        artifacts=(),
        source_hashes=(SourceObjectHash("bootstrap-static", "a" * 64),),
    )


def canonical_snapshot(
    *,
    snapshot_id: str = "snapshot-a",
    observed_at: datetime = OBSERVED_AT,
) -> CanonicalSnapshot:
    prepared = prepare_snapshot(FIXTURE_ROOT)
    mapped = map_snapshot(prepared)
    nullable_player = Player(
        id=mapped.players[0].id,
        team_id=mapped.players[0].team_id,
        first_name=mapped.players[0].first_name,
        last_name=mapped.players[0].last_name,
        web_name=mapped.players[0].web_name,
        position=Position.GOALKEEPER,
        price=Money(tenths_million=55),
        external_refs=(),
    )
    nullable_fixture = Fixture(
        id=mapped.fixtures[0].id,
        home_team_id=mapped.fixtures[0].home_team_id,
        away_team_id=mapped.fixtures[0].away_team_id,
        kickoff_at=None,
        gameweek=None,
        external_refs=mapped.fixtures[0].external_refs,
    )
    non_utc_deadline = mapped.gameweeks[0].model_copy(
        update={"deadline_at": datetime(2026, 8, 14, 13, 30, tzinfo=timezone(timedelta(hours=1)))}
    )
    return CanonicalSnapshot(
        provider_id="synthetic-fpl",
        season=mapped.season,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        processed_at=PROCESSED_AT,
        teams=mapped.teams,
        players=(nullable_player, mapped.players[1]),
        gameweeks=(non_utc_deadline,),
        fixtures=(nullable_fixture,),
        source_hashes=tuple(
            SourceObjectHash(item.resource_name, item.sha256) for item in prepared.objects
        ),
        source_reference="data/raw/synthetic-fpl/2026-27/source",
        code_revision="deadbeef",
    )


def repositories(tmp_path: Path) -> tuple[DuckDbSnapshotCatalog, ParquetCanonicalRepository, Path]:
    database = tmp_path / "state" / "fpl.duckdb"
    catalog = DuckDbSnapshotCatalog(database)
    return catalog, ParquetCanonicalRepository(tmp_path / "data" / "curated", catalog), database


def test_snapshot_catalog_register_get_list_and_idempotency(tmp_path: Path) -> None:
    catalog = DuckDbSnapshotCatalog(tmp_path / "state" / "fpl.duckdb")
    item = registration()

    catalog.register(item)
    catalog.register(item)

    assert catalog.get("synthetic-fpl", "2026-27", "snapshot-a") == item
    assert catalog.list(provider_id="synthetic-fpl", season="2026-27") == (item,)


def test_snapshot_catalog_rejects_conflicting_immutable_registration(tmp_path: Path) -> None:
    catalog = DuckDbSnapshotCatalog(tmp_path / "state" / "fpl.duckdb")
    catalog.register(registration())

    with pytest.raises(ImmutableRegistrationConflict):
        catalog.register(replace(registration(), processed_at=PROCESSED_AT + timedelta(seconds=1)))


def test_canonical_parquet_round_trip_preserves_explicit_types_and_nulls(
    tmp_path: Path,
) -> None:
    catalog, repository, database = repositories(tmp_path)
    original = canonical_snapshot()

    saved = repository.save(original)
    loaded = repository.load(original.provider_id, original.season, original.snapshot_id)

    assert loaded.teams == original.teams
    assert loaded.players == original.players
    assert isinstance(loaded.players[0].id, UUID)
    assert loaded.players[0].price.tenths_million == 55
    assert loaded.players[0].position is Position.GOALKEEPER
    assert loaded.players[0].external_refs == ()
    assert loaded.gameweeks[0].deadline_at.tzinfo is not None
    assert loaded.gameweeks[0].deadline_at.utcoffset() == timedelta(0)
    assert loaded.gameweeks[0].deadline_at == original.gameweeks[0].deadline_at
    assert loaded.fixtures[0].home_team_id == original.fixtures[0].home_team_id
    assert loaded.fixtures[0].away_team_id == original.fixtures[0].away_team_id
    assert loaded.fixtures[0].kickoff_at is None
    assert loaded.fixtures[0].gameweek is None
    assert {artifact.dataset for artifact in saved.artifacts} == set(CanonicalDatasetName)
    assert all(Path(artifact.path).is_file() for artifact in saved.artifacts)
    assert database.is_file()
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("SELECT count(*) FROM players_history").fetchone() == (2,)
        row = connection.execute(
            "SELECT schema_version, source_id FROM players_history WHERE id = ?",
            [str(original.players[0].id)],
        ).fetchone()
    assert row == (1, None)
    assert catalog.get(original.provider_id, original.season, original.snapshot_id) == saved


def test_future_parquet_schema_version_is_rejected(tmp_path: Path) -> None:
    _, repository, _ = repositories(tmp_path)
    original = canonical_snapshot()
    saved = repository.save(original)
    teams_path = next(
        Path(item.path) for item in saved.artifacts if item.dataset is CanonicalDatasetName.TEAMS
    )
    frame = pl.read_parquet(teams_path, hive_partitioning=False).with_columns(
        pl.lit(2, dtype=pl.Int64).alias("schema_version")
    )
    frame.write_parquet(teams_path)

    with pytest.raises(UnsupportedSchemaVersion, match="schema_version 2"):
        repository.load(original.provider_id, original.season, original.snapshot_id)


def test_multiple_snapshots_preserve_history_and_latest_uses_stable_tie_breaker(
    tmp_path: Path,
) -> None:
    _, repository, database = repositories(tmp_path)
    first = canonical_snapshot(snapshot_id="snapshot-a")
    second_player = first.players[1].model_copy(update={"price": Money(tenths_million=81)})
    second = replace(
        first,
        snapshot_id="snapshot-b",
        processed_at=PROCESSED_AT + timedelta(minutes=1),
        players=(first.players[0], second_player),
    )

    repository.save(first)
    repository.save(second)

    with duckdb.connect(str(database)) as connection:
        assert connection.execute("SELECT count(*) FROM players_history").fetchone() == (4,)
        latest = connection.execute(
            "SELECT source_snapshot_id, price_tenths_million FROM players_latest WHERE id = ?",
            [str(second_player.id)],
        ).fetchone()
    assert latest == ("snapshot-b", 81)
    assert repository.load(first.provider_id, first.season, first.snapshot_id).players[1].price == (
        first.players[1].price
    )


def test_failed_atomic_validation_does_not_register_or_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, repository, _ = repositories(tmp_path)
    original = canonical_snapshot()

    def fail_validation(*args: object, **kwargs: object) -> pl.DataFrame:
        raise PersistenceError("forced validation failure")

    monkeypatch.setattr(repository, "_validate_file", fail_validation)
    with pytest.raises(PersistenceError, match="forced validation"):
        repository.save(original)

    assert catalog.get(original.provider_id, original.season, original.snapshot_id) is None
    assert not list((tmp_path / "data" / "curated").rglob("*.parquet"))


def test_issue_3_snapshot_maps_persists_and_queries_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in persistence integration tests")

    monkeypatch.setattr(socket, "socket", network_forbidden)
    prepared = prepare_snapshot(FIXTURE_ROOT)
    mapped = map_snapshot(prepared)
    stored = SnapshotStore(tmp_path / "data" / "raw").store(
        prepared.with_season(mapped.season), imported_at=PROCESSED_AT
    )
    provider = FplSnapshotProvider(
        mapped,
        provider_id=stored.manifest.provider_id,
        snapshot_id=stored.manifest.snapshot_id,
        observed_at=stored.manifest.observed_at,
        imported_at=stored.manifest.imported_at,
        source_reference=str(stored.path),
    )
    _, repository, database = repositories(tmp_path)

    result = persist_provider_snapshot(
        provider,
        repository,
        season=mapped.season,
        processed_at=stored.manifest.processed_at,
        published_at=stored.manifest.published_at,
        source_reference=str(stored.path),
        code_revision=stored.manifest.code_revision,
        source_hashes=tuple(
            SourceObjectHash(item.resource_name, item.sha256)
            for item in stored.manifest.source_objects
        ),
    )

    assert len(result.artifacts) == 4
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("SELECT name FROM teams_latest ORDER BY name").fetchall() == [
            ("North London Reds",),
            ("South Coast Blues",),
        ]


def test_decision_run_round_trip_and_snapshot_provenance_are_distinguishable(
    tmp_path: Path,
) -> None:
    repository = DuckDbDecisionRunRepository(tmp_path / "state" / "fpl.duckdb")
    first = DecisionRun(
        id=uuid4(),
        created_at=PROCESSED_AT,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        code_revision="deadbeef",
        source_is_dirty=False,
        config_fingerprint="sha256:config",
        effective_config_reference="config/effective.yaml",
        input_snapshot_ids=("snapshot-a",),
        projection_versions=("projection-provider:model-v1",),
        optimiser_engine="highs",
        optimiser_version="1.12",
        optimiser_settings_reference="config/optimiser.yaml",
        optimiser_settings=(("mip_gap", "0.001"), ("time_limit", "30")),
        strategy_mode="balanced",
        objective_mode="expected_points",
        random_seed=7,
        simulation_count=1000,
        output_artifact_references=("data/derived/run-a.parquet",),
        status=DecisionRunStatus.SUCCEEDED,
        diagnostic_summary="optimal",
    )
    second = first.model_copy(update={"id": uuid4(), "input_snapshot_ids": ("snapshot-b",)})

    repository.save(first)
    repository.save(first)
    repository.save(second)

    assert repository.get(first.id) == first
    assert repository.get(second.id) == second
    assert (
        repository.get(first.id).input_snapshot_ids != repository.get(second.id).input_snapshot_ids
    )


def test_domain_and_application_do_not_import_storage_libraries() -> None:
    root = Path(__file__).parents[2] / "src" / "fpl_decision_engine"
    for package in (root / "domain", root / "application"):
        source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        assert "import duckdb" not in source
        assert "import polars" not in source


def test_runtime_data_and_state_are_git_ignored() -> None:
    root = Path(__file__).parents[2]
    result = subprocess.run(
        ["git", "check-ignore", "data/curated/example.parquet", "state/fpl.duckdb"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        "data/curated/example.parquet",
        "state/fpl.duckdb",
    ]
