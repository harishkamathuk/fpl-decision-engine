"""Acceptance tests for the #93 narrow chronology boundary."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fpl_decision_engine.application import assess_chronology_input, assess_observation_chronology
from fpl_decision_engine.domain import (
    ChronologyInput,
    ChronologyReason,
    ChronologyStatus,
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    Projection,
)

CUTOFF = datetime(2026, 8, 30, 12, tzinfo=UTC)
PLAYER = UUID(int=93001)


def observation() -> LineupEvidenceValidationObservation:
    projection = Projection(
        player_id=PLAYER,
        gameweek=GameweekNumber(value=1),
        expected_points=5,
        expected_minutes=80,
        appearance_probability=0.9,
        start_probability=0.5,
        source="fpl-forecast",
        model_version="v1",
        generated_at=CUTOFF - timedelta(minutes=2),
    )
    evidence = LineupEvidenceProvenance(
        provider_id="lineup",
        provider_version="v1",
        source_reference="fixture://evidence",
        raw_sha256="a" * 64,
        observed_at=CUTOFF - timedelta(minutes=1),
        retrieved_at=CUTOFF - timedelta(minutes=1),
    )
    return LineupEvidenceValidationObservation.from_projection(
        season="2026-27",
        projection=projection,
        projection_provider_version="v1",
        projection_source_reference="fixture://projection",
        projection_source_sha256="b" * 64,
        projection_snapshot_id="projection-1",
        projection_mapping_fingerprint="c" * 64,
        evidence_status=LineupEvidenceStatus.CLASSIFIED,
        evidence_class=LineupEvidenceClass.SUPPORTS_START,
        evidence=evidence,
    )


def test_missing_projection_timestamp_is_unproven() -> None:
    decision = assess_chronology_input(
        ChronologyInput(
            projection_generated_at=None,
            evidence_observed_at=CUTOFF - timedelta(minutes=1),
        ),
        cutoff=CUTOFF,
    )
    assert decision.status is ChronologyStatus.EXCLUDED_CHRONOLOGY_UNPROVEN
    assert ChronologyReason.PROJECTION_TIMESTAMP_MISSING in decision.reasons


def test_unproven_evidence_is_excluded() -> None:
    decision = assess_chronology_input(
        ChronologyInput(
            projection_generated_at=CUTOFF - timedelta(minutes=2),
            evidence_observed_at=CUTOFF - timedelta(minutes=1),
            chronology_proven=False,
        ),
        cutoff=CUTOFF,
    )
    assert decision.status is ChronologyStatus.EXCLUDED_CHRONOLOGY_UNPROVEN
    assert ChronologyReason.EVIDENCE_CHRONOLOGY_UNPROVEN in decision.reasons


def test_invalid_timestamp_is_excluded() -> None:
    decision = assess_chronology_input(
        ChronologyInput(
            projection_generated_at=datetime(2026, 8, 30, 11),
            evidence_observed_at=CUTOFF - timedelta(minutes=1),
        ),
        cutoff=CUTOFF,
    )
    assert decision.status is ChronologyStatus.EXCLUDED_CHRONOLOGY_UNPROVEN
    assert ChronologyReason.TIMESTAMP_INVALID in decision.reasons


def test_processed_after_cutoff_does_not_invalidate_proven_capture() -> None:
    decision = assess_chronology_input(
        ChronologyInput(
            projection_generated_at=CUTOFF - timedelta(minutes=2),
            evidence_observed_at=CUTOFF - timedelta(minutes=1),
            evidence_processed_at=CUTOFF + timedelta(minutes=5),
        ),
        cutoff=CUTOFF,
    )
    assert decision.status is ChronologyStatus.VALID


def test_complete_observation_uses_same_boundary() -> None:
    assert (
        assess_observation_chronology(observation(), cutoff=CUTOFF).status
        is ChronologyStatus.VALID
    )
