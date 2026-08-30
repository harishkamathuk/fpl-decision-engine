"""Acceptance tests for the #93 narrow chronology boundary."""

from datetime import datetime, timedelta

from fpl_decision_engine.application import assess_chronology_input, assess_observation_chronology
from fpl_decision_engine.domain import (
    ChronologyInput,
    ChronologyReason,
    ChronologyStatus,
)
from tests.application.test_lineup_outcomes import CUTOFF, observation


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
