"""Acceptance tests for realised lineup outcomes and chronology decisions."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.application import assess_observation_chronology
from fpl_decision_engine.domain import (
    ChronologyReason,
    ChronologyStatus,
    ExternalRef,
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    Projection,
)

CUTOFF = datetime(2026, 8, 30, 12, tzinfo=UTC)
PLAYER = UUID(int=93001)


def observation(
    *,
    generated=CUTOFF - timedelta(minutes=2),
    observed=CUTOFF - timedelta(minutes=1),
    published=None,
):
    projection = Projection(
        player_id=PLAYER, gameweek=GameweekNumber(value=1), expected_points=5,
        expected_minutes=80, appearance_probability=.9, start_probability=.5,
        source="fpl-forecast", model_version="v1", generated_at=generated,
    )
    evidence = LineupEvidenceProvenance(
        provider_id="lineup", provider_version="v1", source_reference="fixture://evidence",
        raw_sha256="a" * 64, observed_at=observed, retrieved_at=observed,
        published_at=published,
    )
    return LineupEvidenceValidationObservation.from_projection(
        season="2026-27", projection=projection, projection_provider_version="v1",
        projection_source_reference="fixture://projection", projection_source_sha256="b" * 64,
        projection_snapshot_id="projection-1", projection_mapping_fingerprint="c" * 64,
        evidence_status=LineupEvidenceStatus.CLASSIFIED,
        evidence_class=LineupEvidenceClass.SUPPORTS_START, evidence=evidence,
    )


def test_before_cutoff_is_valid_and_equality_is_excluded() -> None:
    assert (
        assess_observation_chronology(observation(), cutoff=CUTOFF).status
        is ChronologyStatus.VALID
    )
    decision = assess_observation_chronology(
        observation(generated=CUTOFF), cutoff=CUTOFF
    )
    assert decision.status is ChronologyStatus.EXCLUDED_CHRONOLOGY
    assert ChronologyReason.PROJECTION_AT_OR_AFTER_CUTOFF in decision.reasons


def test_late_observed_or_published_evidence_is_excluded() -> None:
    observed = assess_observation_chronology(
        observation(observed=CUTOFF), cutoff=CUTOFF
    )
    published = assess_observation_chronology(
        observation(published=CUTOFF), cutoff=CUTOFF
    )
    assert ChronologyReason.EVIDENCE_OBSERVED_AT_OR_AFTER_CUTOFF in observed.reasons
    assert ChronologyReason.EVIDENCE_PUBLISHED_AT_OR_AFTER_CUTOFF in published.reasons


def test_outcome_contract_rejects_nonstart_with_minutes() -> None:
    from fpl_decision_engine.domain import RealisedOutcome

    with pytest.raises(ValidationError, match="zero minutes"):
        RealisedOutcome(
            season="2026-27", gameweek=GameweekNumber(value=1),
            player_ref=ExternalRef(provider="fpl-element", external_id="93001"),
            canonical_player_id=PLAYER, started=False, minutes=1,
            source_reference="fixture://live", provider_id="fpl", provider_version="api",
            snapshot_id="snap", retrieved_at=CUTOFF, finalised_at=CUTOFF,
        )
