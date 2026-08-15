"""DuckDB catalogue and Parquet view registration for immutable snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection

from fpl_decision_engine.ports.persistence import (
    CanonicalDatasetName,
    DatasetArtifact,
    ImmutableRegistrationConflict,
    SnapshotRegistration,
    SourceObjectHash,
)

_ENTITY_KEYS = {
    CanonicalDatasetName.TEAMS: ("provider_id", "season", "id"),
    CanonicalDatasetName.PLAYERS: ("provider_id", "season", "id"),
    CanonicalDatasetName.GAMEWEEKS: ("provider_id", "season", "gameweek_number"),
    CanonicalDatasetName.FIXTURES: ("provider_id", "season", "id"),
}


def _sql_string(value: str) -> str:
    """Quote a path as a DuckDB string literal rather than interpolating executable SQL."""

    return "'" + value.replace("'", "''") + "'"


class DuckDbSnapshotCatalog:
    """Store small immutable catalogue records and views, never canonical fact rows."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._create_schema(connection)

    def _connect(self) -> DuckDBPyConnection:
        return duckdb.connect(str(self.database_path))

    @staticmethod
    def _create_schema(connection: DuckDBPyConnection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_catalog (
                provider_id VARCHAR NOT NULL,
                season VARCHAR NOT NULL,
                snapshot_id VARCHAR NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                processed_at TIMESTAMPTZ NOT NULL,
                published_at TIMESTAMPTZ,
                schema_version INTEGER NOT NULL,
                source_reference VARCHAR,
                code_revision VARCHAR,
                PRIMARY KEY (provider_id, season, snapshot_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_artifacts (
                provider_id VARCHAR NOT NULL,
                season VARCHAR NOT NULL,
                snapshot_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                path VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                PRIMARY KEY (provider_id, season, snapshot_id, dataset)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_object_hashes (
                provider_id VARCHAR NOT NULL,
                season VARCHAR NOT NULL,
                snapshot_id VARCHAR NOT NULL,
                resource_name VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                PRIMARY KEY (provider_id, season, snapshot_id, resource_name)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                dataset VARCHAR NOT NULL,
                schema_version INTEGER NOT NULL,
                description VARCHAR NOT NULL,
                PRIMARY KEY (dataset, schema_version)
            )
            """
        )
        connection.executemany(
            "INSERT OR IGNORE INTO schema_metadata VALUES (?, ?, ?)",
            [
                (dataset.value, 1, "Initial explicit canonical Parquet schema")
                for dataset in CanonicalDatasetName
            ],
        )

    def register(self, registration: SnapshotRegistration) -> None:
        """Register one immutable snapshot atomically with all view updates.

        Repeating an exactly equal registration is a no-op. Reusing its
        provider/season/snapshot identity with any different metadata is rejected.
        """

        with self._connect() as connection:
            connection.begin()
            try:
                existing = self._get(connection, *self._key(registration))
                if existing is not None:
                    if existing != registration:
                        raise ImmutableRegistrationConflict(
                            "immutable snapshot registration conflicts with existing metadata"
                        )
                    connection.rollback()
                    return
                connection.execute(
                    """
                    INSERT INTO snapshot_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        registration.provider_id,
                        registration.season,
                        registration.snapshot_id,
                        registration.observed_at,
                        registration.processed_at,
                        registration.published_at,
                        registration.schema_version,
                        registration.source_reference,
                        registration.code_revision,
                    ],
                )
                if registration.artifacts:
                    connection.executemany(
                        "INSERT INTO dataset_artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                registration.provider_id,
                                registration.season,
                                registration.snapshot_id,
                                artifact.dataset.value,
                                artifact.path,
                                artifact.sha256,
                                artifact.row_count,
                            )
                            for artifact in registration.artifacts
                        ],
                    )
                if registration.source_hashes:
                    connection.executemany(
                        "INSERT INTO source_object_hashes VALUES (?, ?, ?, ?, ?)",
                        [
                            (
                                registration.provider_id,
                                registration.season,
                                registration.snapshot_id,
                                source_hash.resource_name,
                                source_hash.sha256,
                            )
                            for source_hash in registration.source_hashes
                        ],
                    )
                self._refresh_views(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _key(registration: SnapshotRegistration) -> tuple[str, str, str]:
        return registration.provider_id, registration.season, registration.snapshot_id

    def get(self, provider_id: str, season: str, snapshot_id: str) -> SnapshotRegistration | None:
        with self._connect() as connection:
            return self._get(connection, provider_id, season, snapshot_id)

    def list(
        self, *, provider_id: str | None = None, season: str | None = None
    ) -> tuple[SnapshotRegistration, ...]:
        predicates: list[str] = []
        parameters: list[str] = []
        if provider_id is not None:
            predicates.append("provider_id = ?")
            parameters.append(provider_id)
        if season is not None:
            predicates.append("season = ?")
            parameters.append(season)
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        with self._connect() as connection:
            keys = connection.execute(
                "SELECT provider_id, season, snapshot_id FROM snapshot_catalog"
                + where
                + " ORDER BY observed_at, provider_id, season, snapshot_id",
                parameters,
            ).fetchall()
            return tuple(
                registration
                for key in keys
                if (registration := self._get(connection, *key)) is not None
            )

    def _get(
        self, connection: DuckDBPyConnection, provider_id: str, season: str, snapshot_id: str
    ) -> SnapshotRegistration | None:
        row = connection.execute(
            """
            SELECT CAST(observed_at AS VARCHAR), CAST(processed_at AS VARCHAR),
                   CAST(published_at AS VARCHAR), schema_version,
                   source_reference, code_revision
            FROM snapshot_catalog
            WHERE provider_id = ? AND season = ? AND snapshot_id = ?
            """,
            [provider_id, season, snapshot_id],
        ).fetchone()
        if row is None:
            return None
        artifact_rows = connection.execute(
            """
            SELECT dataset, path, sha256, row_count
            FROM dataset_artifacts
            WHERE provider_id = ? AND season = ? AND snapshot_id = ?
            ORDER BY dataset
            """,
            [provider_id, season, snapshot_id],
        ).fetchall()
        hash_rows = connection.execute(
            """
            SELECT resource_name, sha256
            FROM source_object_hashes
            WHERE provider_id = ? AND season = ? AND snapshot_id = ?
            ORDER BY resource_name
            """,
            [provider_id, season, snapshot_id],
        ).fetchall()
        return SnapshotRegistration(
            provider_id=provider_id,
            season=season,
            snapshot_id=snapshot_id,
            observed_at=datetime.fromisoformat(row[0]),
            processed_at=datetime.fromisoformat(row[1]),
            published_at=datetime.fromisoformat(row[2]) if row[2] is not None else None,
            schema_version=row[3],
            source_reference=row[4],
            code_revision=row[5],
            artifacts=tuple(
                DatasetArtifact(
                    dataset=CanonicalDatasetName(item[0]),
                    path=item[1],
                    sha256=item[2],
                    row_count=item[3],
                )
                for item in artifact_rows
            ),
            source_hashes=tuple(SourceObjectHash(*item) for item in hash_rows),
        )

    @staticmethod
    def _refresh_views(connection: DuckDBPyConnection) -> None:
        """Rebuild history/latest views from registered immutable artifact paths."""

        for dataset, entity_keys in _ENTITY_KEYS.items():
            rows = connection.execute(
                "SELECT path FROM dataset_artifacts WHERE dataset = ? ORDER BY path",
                [dataset.value],
            ).fetchall()
            if not rows:
                continue
            paths = ", ".join(_sql_string(str(Path(row[0]).resolve())) for row in rows)
            history_view = f"{dataset.value}_history"
            latest_view = f"{dataset.value}_latest"
            partition = ", ".join(entity_keys)
            connection.execute(
                f"CREATE OR REPLACE VIEW {history_view} AS "
                f"SELECT * FROM read_parquet([{paths}], union_by_name = true)"
            )
            connection.execute(
                f"""
                CREATE OR REPLACE VIEW {latest_view} AS
                SELECT * EXCLUDE (_latest_rank)
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY {partition}
                        ORDER BY observed_at DESC, source_snapshot_id DESC
                    ) AS _latest_rank
                    FROM {history_view}
                )
                WHERE _latest_rank = 1
                """
            )
