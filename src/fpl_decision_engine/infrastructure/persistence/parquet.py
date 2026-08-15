"""Explicit Polars/Parquet persistence for canonical snapshot-grain datasets."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from fpl_decision_engine.domain import (
    ExternalRef,
    Fixture,
    Gameweek,
    GameweekNumber,
    Money,
    Player,
    Position,
    Team,
)
from fpl_decision_engine.ports.persistence import (
    CanonicalDatasetName,
    CanonicalSnapshot,
    DatasetArtifact,
    ImmutableRegistrationConflict,
    PersistenceError,
    SnapshotRegistration,
    UnsupportedSchemaVersion,
)

from .catalog import DuckDbSnapshotCatalog

SCHEMA_VERSION = 1
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UTC_DATETIME = pl.Datetime(time_unit="us", time_zone="UTC")
_PROVENANCE_SCHEMA = {
    "season": pl.String,
    "provider_id": pl.String,
    "source_snapshot_id": pl.String,
    "source_id": pl.String,
    "observed_at": _UTC_DATETIME,
    "processed_at": _UTC_DATETIME,
    "published_at": _UTC_DATETIME,
    "schema_version": pl.Int64,
}
_SCHEMAS: dict[CanonicalDatasetName, pl.Schema] = {
    CanonicalDatasetName.TEAMS: pl.Schema(
        {
            "id": pl.String,
            "name": pl.String,
            "short_name": pl.String,
            "external_ref_providers": pl.List(pl.String),
            "external_ref_ids": pl.List(pl.String),
            **_PROVENANCE_SCHEMA,
        }
    ),
    CanonicalDatasetName.PLAYERS: pl.Schema(
        {
            "id": pl.String,
            "team_id": pl.String,
            "first_name": pl.String,
            "last_name": pl.String,
            "web_name": pl.String,
            "position": pl.String,
            # FPL prices are exact integer tenths of £1m; no binary float is stored.
            "price_tenths_million": pl.Int64,
            "active": pl.Boolean,
            "external_ref_providers": pl.List(pl.String),
            "external_ref_ids": pl.List(pl.String),
            **_PROVENANCE_SCHEMA,
        }
    ),
    CanonicalDatasetName.GAMEWEEKS: pl.Schema(
        {
            "gameweek_number": pl.Int64,
            "name": pl.String,
            "deadline_at": _UTC_DATETIME,
            "finished": pl.Boolean,
            **_PROVENANCE_SCHEMA,
        }
    ),
    CanonicalDatasetName.FIXTURES: pl.Schema(
        {
            "id": pl.String,
            "home_team_id": pl.String,
            "away_team_id": pl.String,
            "kickoff_at": _UTC_DATETIME,
            "gameweek_number": pl.Int64,
            "external_ref_providers": pl.List(pl.String),
            "external_ref_ids": pl.List(pl.String),
            **_PROVENANCE_SCHEMA,
        }
    ),
}


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def _external_refs(refs: tuple[ExternalRef, ...]) -> tuple[list[str], list[str]]:
    return [ref.provider for ref in refs], [ref.external_id for ref in refs]


def _source_id(refs: tuple[ExternalRef, ...], provider_id: str) -> str | None:
    return next((ref.external_id for ref in refs if ref.provider == provider_id), None)


def _provenance(snapshot: CanonicalSnapshot, source_id: str | None) -> dict[str, Any]:
    return {
        "season": snapshot.season,
        "provider_id": snapshot.provider_id,
        "source_snapshot_id": snapshot.snapshot_id,
        "source_id": source_id,
        "observed_at": _utc(snapshot.observed_at),
        "processed_at": _utc(snapshot.processed_at),
        "published_at": _utc(snapshot.published_at),
        "schema_version": SCHEMA_VERSION,
    }


def _team_rows(snapshot: CanonicalSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for team in snapshot.teams:
        providers, ids = _external_refs(team.external_refs)
        rows.append(
            {
                "id": str(team.id),
                "name": team.name,
                "short_name": team.short_name,
                "external_ref_providers": providers,
                "external_ref_ids": ids,
                **_provenance(snapshot, _source_id(team.external_refs, snapshot.provider_id)),
            }
        )
    return rows


def _player_rows(snapshot: CanonicalSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for player in snapshot.players:
        providers, ids = _external_refs(player.external_refs)
        rows.append(
            {
                "id": str(player.id),
                "team_id": str(player.team_id),
                "first_name": player.first_name,
                "last_name": player.last_name,
                "web_name": player.web_name,
                "position": player.position.value,
                "price_tenths_million": player.price.tenths_million,
                "active": player.active,
                "external_ref_providers": providers,
                "external_ref_ids": ids,
                **_provenance(snapshot, _source_id(player.external_refs, snapshot.provider_id)),
            }
        )
    return rows


def _gameweek_rows(snapshot: CanonicalSnapshot) -> list[dict[str, Any]]:
    return [
        {
            "gameweek_number": gameweek.number.value,
            "name": gameweek.name,
            "deadline_at": _utc(gameweek.deadline_at),
            "finished": gameweek.finished,
            **_provenance(snapshot, str(gameweek.number.value)),
        }
        for gameweek in snapshot.gameweeks
    ]


def _fixture_rows(snapshot: CanonicalSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fixture in snapshot.fixtures:
        providers, ids = _external_refs(fixture.external_refs)
        rows.append(
            {
                "id": str(fixture.id),
                "home_team_id": str(fixture.home_team_id),
                "away_team_id": str(fixture.away_team_id),
                "kickoff_at": _utc(fixture.kickoff_at),
                "gameweek_number": fixture.gameweek.value if fixture.gameweek else None,
                "external_ref_providers": providers,
                "external_ref_ids": ids,
                **_provenance(snapshot, _source_id(fixture.external_refs, snapshot.provider_id)),
            }
        )
    return rows


_ROW_MAPPERS: dict[CanonicalDatasetName, Callable[[CanonicalSnapshot], list[dict[str, Any]]]] = {
    CanonicalDatasetName.TEAMS: _team_rows,
    CanonicalDatasetName.PLAYERS: _player_rows,
    CanonicalDatasetName.GAMEWEEKS: _gameweek_rows,
    CanonicalDatasetName.FIXTURES: _fixture_rows,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _refs(row: dict[str, Any]) -> tuple[ExternalRef, ...]:
    return tuple(
        ExternalRef(provider=provider, external_id=external_id)
        for provider, external_id in zip(
            row["external_ref_providers"], row["external_ref_ids"], strict=True
        )
    )


class ParquetCanonicalRepository:
    """Persist explicit canonical schemas to immutable, snapshot-addressable Parquet.

    UUIDs are stored as lowercase canonical text for stable interchange with both Polars
    and DuckDB. Instant-valued datetimes are normalized to UTC and stored as timezone-aware
    microsecond timestamps. Each file is written to a temporary sibling, reopened and
    validated, promoted without overwriting an existing file, and only then catalogued.
    """

    def __init__(self, curated_root: Path, catalog: DuckDbSnapshotCatalog) -> None:
        self.curated_root = curated_root.resolve()
        self.catalog = catalog

    def save(self, snapshot: CanonicalSnapshot) -> SnapshotRegistration:
        """Persist all four datasets; failures never register a partial snapshot."""

        self._validate_components(snapshot)
        existing = self.catalog.get(snapshot.provider_id, snapshot.season, snapshot.snapshot_id)
        if existing is not None:
            self._require_same_source(existing, snapshot)
            for artifact in existing.artifacts:
                self._validate_file(
                    Path(artifact.path), artifact.dataset, expected_rows=artifact.row_count
                )
            return existing

        artifacts: list[DatasetArtifact] = []
        created_paths: list[Path] = []
        try:
            for dataset in CanonicalDatasetName:
                rows = _ROW_MAPPERS[dataset](snapshot)
                artifact, created = self._write_dataset(dataset, snapshot, rows)
                artifacts.append(artifact)
                if created:
                    created_paths.append(Path(artifact.path))
            registration = SnapshotRegistration(
                provider_id=snapshot.provider_id,
                season=snapshot.season,
                snapshot_id=snapshot.snapshot_id,
                observed_at=snapshot.observed_at,
                processed_at=snapshot.processed_at,
                published_at=snapshot.published_at,
                schema_version=SCHEMA_VERSION,
                source_reference=snapshot.source_reference,
                code_revision=snapshot.code_revision,
                artifacts=tuple(sorted(artifacts, key=lambda item: item.dataset.value)),
                source_hashes=tuple(
                    sorted(snapshot.source_hashes, key=lambda item: item.resource_name)
                ),
            )
            self.catalog.register(registration)
            return registration
        except Exception:
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_components(snapshot: CanonicalSnapshot) -> None:
        for field_name, value in (
            ("provider_id", snapshot.provider_id),
            ("season", snapshot.season),
            ("snapshot_id", snapshot.snapshot_id),
        ):
            if not _SAFE_COMPONENT.fullmatch(value):
                raise PersistenceError(f"{field_name} contains unsafe path characters")

    @staticmethod
    def _require_same_source(
        registration: SnapshotRegistration, snapshot: CanonicalSnapshot
    ) -> None:
        values_match = (
            registration.observed_at == snapshot.observed_at
            and registration.processed_at == snapshot.processed_at
            and registration.published_at == snapshot.published_at
            and registration.source_reference == snapshot.source_reference
            and registration.code_revision == snapshot.code_revision
            and registration.source_hashes
            == tuple(sorted(snapshot.source_hashes, key=lambda item: item.resource_name))
        )
        if not values_match:
            raise ImmutableRegistrationConflict(
                "immutable snapshot identity was reused with different source provenance"
            )

    def _write_dataset(
        self,
        dataset: CanonicalDatasetName,
        snapshot: CanonicalSnapshot,
        rows: list[dict[str, Any]],
    ) -> tuple[DatasetArtifact, bool]:
        target = (
            self.curated_root
            / dataset.value
            / f"season={snapshot.season}"
            / f"{snapshot.snapshot_id}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{snapshot.snapshot_id}.", suffix=".parquet", dir=target.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            frame = pl.DataFrame(rows, schema=_SCHEMAS[dataset])
            frame.write_parquet(temporary, compression="zstd", statistics=True)
            self._validate_file(temporary, dataset, expected_rows=len(rows))
            digest = _sha256(temporary)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if _sha256(target) != digest:
                    raise ImmutableRegistrationConflict(
                        f"immutable curated artifact conflicts at {target}"
                    ) from None
                return (
                    DatasetArtifact(dataset, str(target), digest, len(rows)),
                    False,
                )
            return DatasetArtifact(dataset, str(target), digest, len(rows)), True
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_file(
        path: Path, dataset: CanonicalDatasetName, *, expected_rows: int
    ) -> pl.DataFrame:
        """Reopen a generated file and reject schema, count, or version drift."""

        frame = pl.read_parquet(path, hive_partitioning=False)
        if frame.schema != _SCHEMAS[dataset]:
            raise PersistenceError(
                f"{dataset.value} Parquet schema does not match supported version {SCHEMA_VERSION}"
            )
        if frame.height != expected_rows:
            raise PersistenceError(
                f"{dataset.value} Parquet row count {frame.height} != expected {expected_rows}"
            )
        versions = frame.get_column("schema_version").unique().to_list()
        if versions and versions != [SCHEMA_VERSION]:
            version = max(versions)
            raise UnsupportedSchemaVersion(
                f"unsupported {dataset.value} schema_version {version}; "
                f"reader supports {SCHEMA_VERSION}"
            )
        return frame

    def load(self, provider_id: str, season: str, snapshot_id: str) -> CanonicalSnapshot:
        """Reconstruct canonical domain models from one registered snapshot."""

        registration = self.catalog.get(provider_id, season, snapshot_id)
        if registration is None:
            raise PersistenceError("canonical snapshot is not registered")
        if registration.schema_version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"unsupported catalogue schema_version {registration.schema_version}; "
                f"reader supports {SCHEMA_VERSION}"
            )
        artifacts = {artifact.dataset: artifact for artifact in registration.artifacts}
        missing = set(CanonicalDatasetName) - artifacts.keys()
        if missing:
            raise PersistenceError(
                "registered canonical snapshot is missing datasets: "
                + ", ".join(sorted(item.value for item in missing))
            )
        frames = {
            dataset: self._validate_file(
                Path(artifact.path), dataset, expected_rows=artifact.row_count
            )
            for dataset, artifact in artifacts.items()
        }
        return CanonicalSnapshot(
            provider_id=registration.provider_id,
            season=registration.season,
            snapshot_id=registration.snapshot_id,
            observed_at=registration.observed_at,
            processed_at=registration.processed_at,
            published_at=registration.published_at,
            source_reference=registration.source_reference,
            code_revision=registration.code_revision,
            source_hashes=registration.source_hashes,
            teams=self._load_teams(frames[CanonicalDatasetName.TEAMS]),
            players=self._load_players(frames[CanonicalDatasetName.PLAYERS]),
            gameweeks=self._load_gameweeks(frames[CanonicalDatasetName.GAMEWEEKS]),
            fixtures=self._load_fixtures(frames[CanonicalDatasetName.FIXTURES]),
        )

    @staticmethod
    def _rows(frame: pl.DataFrame) -> Iterable[dict[str, Any]]:
        return frame.iter_rows(named=True)

    def _load_teams(self, frame: pl.DataFrame) -> tuple[Team, ...]:
        return tuple(
            Team(
                id=row["id"],
                name=row["name"],
                short_name=row["short_name"],
                external_refs=_refs(row),
            )
            for row in self._rows(frame)
        )

    def _load_players(self, frame: pl.DataFrame) -> tuple[Player, ...]:
        return tuple(
            Player(
                id=row["id"],
                team_id=row["team_id"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                web_name=row["web_name"],
                position=Position(row["position"]),
                price=Money(tenths_million=row["price_tenths_million"]),
                active=row["active"],
                external_refs=_refs(row),
            )
            for row in self._rows(frame)
        )

    def _load_gameweeks(self, frame: pl.DataFrame) -> tuple[Gameweek, ...]:
        return tuple(
            Gameweek(
                number=GameweekNumber(value=row["gameweek_number"]),
                name=row["name"],
                deadline_at=row["deadline_at"],
                finished=row["finished"],
            )
            for row in self._rows(frame)
        )

    def _load_fixtures(self, frame: pl.DataFrame) -> tuple[Fixture, ...]:
        return tuple(
            Fixture(
                id=row["id"],
                home_team_id=row["home_team_id"],
                away_team_id=row["away_team_id"],
                kickoff_at=row["kickoff_at"],
                gameweek=(
                    GameweekNumber(value=row["gameweek_number"])
                    if row["gameweek_number"] is not None
                    else None
                ),
                external_refs=_refs(row),
            )
            for row in self._rows(frame)
        )
