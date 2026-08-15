"""DuckDB persistence for reproducible DecisionRun control metadata."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID

import duckdb

from fpl_decision_engine.domain import DecisionRun, DecisionRunStatus, GameweekNumber
from fpl_decision_engine.ports.persistence import (
    ImmutableRegistrationConflict,
    UnsupportedSchemaVersion,
)

SCHEMA_VERSION = 1


class DuckDbDecisionRunRepository:
    """Persist run provenance as small DuckDB rows, separate from analytical facts."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_runs (
                    run_id VARCHAR PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    season VARCHAR,
                    gameweek INTEGER NOT NULL,
                    code_revision VARCHAR NOT NULL,
                    source_is_dirty BOOLEAN,
                    config_fingerprint VARCHAR NOT NULL,
                    effective_config_reference VARCHAR,
                    input_snapshot_ids VARCHAR[] NOT NULL,
                    projection_versions VARCHAR[] NOT NULL,
                    optimiser_engine VARCHAR,
                    optimiser_version VARCHAR,
                    optimiser_settings_reference VARCHAR,
                    optimiser_setting_keys VARCHAR[] NOT NULL,
                    optimiser_setting_values VARCHAR[] NOT NULL,
                    strategy_mode VARCHAR,
                    objective_mode VARCHAR,
                    random_seed BIGINT,
                    simulation_count BIGINT,
                    output_artifact_references VARCHAR[] NOT NULL,
                    status VARCHAR,
                    diagnostic_summary VARCHAR
                )
                """
            )

    def save(self, run: DecisionRun) -> None:
        """Insert one immutable run, accepting only an identical duplicate."""

        existing = self.get(run.id)
        if existing is not None:
            if existing != run:
                raise ImmutableRegistrationConflict(
                    "decision run ID conflicts with existing provenance"
                )
            return
        with duckdb.connect(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO decision_runs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    str(run.id),
                    SCHEMA_VERSION,
                    run.created_at,
                    run.season,
                    run.gameweek.value,
                    run.code_revision,
                    run.source_is_dirty,
                    run.config_fingerprint,
                    run.effective_config_reference,
                    list(run.input_snapshot_ids),
                    list(run.projection_versions),
                    run.optimiser_engine,
                    run.optimiser_version,
                    run.optimiser_settings_reference,
                    [item[0] for item in run.optimiser_settings],
                    [item[1] for item in run.optimiser_settings],
                    run.strategy_mode,
                    run.objective_mode,
                    run.random_seed,
                    run.simulation_count,
                    list(run.output_artifact_references),
                    run.status.value if run.status is not None else None,
                    run.diagnostic_summary,
                ],
            )

    def get(self, run_id: UUID) -> DecisionRun | None:
        with duckdb.connect(str(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT run_id, schema_version, CAST(created_at AS VARCHAR), season, gameweek,
                       code_revision, source_is_dirty, config_fingerprint,
                       effective_config_reference, input_snapshot_ids, projection_versions,
                       optimiser_engine, optimiser_version, optimiser_settings_reference,
                       optimiser_setting_keys, optimiser_setting_values, strategy_mode,
                       objective_mode, random_seed, simulation_count,
                       output_artifact_references, status, diagnostic_summary
                FROM decision_runs WHERE run_id = ?
                """,
                [str(run_id)],
            ).fetchone()
        if row is None:
            return None
        if row[1] != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"unsupported DecisionRun schema_version {row[1]}; reader supports {SCHEMA_VERSION}"
            )
        return DecisionRun(
            id=row[0],
            created_at=datetime.fromisoformat(row[2]),
            season=row[3],
            gameweek=GameweekNumber(value=row[4]),
            code_revision=row[5],
            source_is_dirty=row[6],
            config_fingerprint=row[7],
            effective_config_reference=row[8],
            input_snapshot_ids=tuple(row[9]),
            projection_versions=tuple(row[10]),
            optimiser_engine=row[11],
            optimiser_version=row[12],
            optimiser_settings_reference=row[13],
            optimiser_settings=tuple(zip(row[14], row[15], strict=True)),
            strategy_mode=row[16],
            objective_mode=row[17],
            random_seed=row[18],
            simulation_count=row[19],
            output_artifact_references=tuple(row[20]),
            status=DecisionRunStatus(row[21]) if row[21] is not None else None,
            diagnostic_summary=row[22],
        )
