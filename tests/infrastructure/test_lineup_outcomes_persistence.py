"""Acceptance tests for joined outcome persistence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fpl_decision_engine.application import join_lineup_outcomes
from fpl_decision_engine.domain import (
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    Projection,
)
from fpl_decision_engine.infrastructure.persistence import FileJoinedLineupOutcomeRepository

CUTOFF = datetime(2026, 8, 30, 12, tzinfo=UTC)
PLAYER = UUID(int=93001)


def observation():
    projection = Projection(
        player_id=PLAYER, gameweek=GameweekNumber(value=1), expected_points=5,
        expected_minutes=80, appearance_probability=.9, start_probability=.5,
        source="fpl-forecast", model_version="v1", generated_at=CUTOFF - timedelta(minutes=2),
    )
    evidence = LineupEvidenceProvenance(
        provider_id="lineup", provider_version="v1", source_reference="fixture://evidence",
        raw_sha256="a" * 64, observed_at=CUTOFF - timedelta(minutes=1),
        retrieved_at=CUTOFF - timedelta(minutes=1),
    )
    return LineupEvidenceValidationObservation.from_projection(
        season="2026-27", projection=projection, projection_provider_version="v1",
        projection_source_reference="fixture://projection", projection_source_sha256="b" * 64,
        projection_snapshot_id="projection-1", projection_mapping_fingerprint="c" * 64,
        evidence_status=LineupEvidenceStatus.CLASSIFIED,
        evidence_class=LineupEvidenceClass.SUPPORTS_START, evidence=evidence,
    )


def test_joined_records_round_trip_deterministically(tmp_path: Path) -> None:
    records = join_lineup_outcomes(
        [observation()], {}, cutoff=CUTOFF, outcome_source_reference="fixture://live",
        outcome_provider_id="fpl", outcome_provider_version="api-v1",
        outcome_snapshot_id="snap", outcome_retrieved_at=CUTOFF,
        outcome_finalised_at=CUTOFF,
    )
    repository = FileJoinedLineupOutcomeRepository(tmp_path)
    repository.save_all(records)
    first = repository.load_all("2026-27", 1)
    repository.save_all(records)
    assert repository.load_all("2026-27", 1) == first == records
