"""Acceptance tests for the realised outcome join."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fpl_decision_engine.application import join_lineup_outcomes, valid_joined_outcomes
from fpl_decision_engine.domain import (
    ExternalRef,
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    OutcomeState,
    Projection,
    RealisedOutcome,
)

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


def outcome(*, player=PLAYER, started=False, minutes=0):
    return RealisedOutcome(
        season="2026-27", gameweek=GameweekNumber(value=1),
        player_ref=ExternalRef(provider="fpl-element", external_id=str(player.int)),
        canonical_player_id=player, started=started, minutes=minutes,
        source_reference="fixture://event/1/live", provider_id="fpl", provider_version="api-v1",
        snapshot_id="outcome-1", retrieved_at=CUTOFF, finalised_at=CUTOFF,
    )


def test_join_distinguishes_nonstart_and_missing_and_is_stable() -> None:
    records = join_lineup_outcomes(
        [observation()], {}, cutoff=CUTOFF, outcome_source_reference="fixture://live",
        outcome_provider_id="fpl", outcome_snapshot_id="snap", outcome_retrieved_at=CUTOFF,
        outcome_finalised_at=CUTOFF,
    )
    assert records[0].outcome_state is OutcomeState.MISSING
    assert valid_joined_outcomes(records)[0].outcome_state is OutcomeState.MISSING

    started = outcome(started=True, minutes=90)
    joined = join_lineup_outcomes(
        [observation()], {("2026-27", 1, PLAYER): started}, cutoff=CUTOFF,
        outcome_source_reference="fixture://live", outcome_provider_id="fpl",
        outcome_snapshot_id="snap", outcome_retrieved_at=CUTOFF, outcome_finalised_at=CUTOFF,
    )
    assert joined[0].outcome_state is OutcomeState.STARTED


def test_join_rejects_duplicate_observations() -> None:
    try:
        join_lineup_outcomes(
            [observation(), observation()], {}, cutoff=CUTOFF,
            outcome_source_reference="fixture://live", outcome_provider_id="fpl",
            outcome_snapshot_id="snap", outcome_retrieved_at=CUTOFF,
            outcome_finalised_at=CUTOFF,
        )
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate observations must fail")
