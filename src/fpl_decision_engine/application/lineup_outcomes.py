"""Application logic for joining frozen observations to official realised outcomes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from fpl_decision_engine.domain import (
    ChronologyDecision,
    ChronologyInput,
    ChronologyReason,
    ChronologyStatus,
    JoinedLineupOutcome,
    LineupEvidenceValidationObservation,
    MissingRealisedOutcome,
    OutcomeState,
    RealisedOutcome,
)


def assess_chronology_input(
    chronology_input: ChronologyInput,
    *,
    cutoff: datetime,
) -> ChronologyDecision:
    """Apply the strict pre-deadline eligibility contract without repairing data."""

    reasons: list[ChronologyReason] = []
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        reasons.append(ChronologyReason.TIMESTAMP_INVALID)
    if chronology_input.timestamp_invalid:
        reasons.append(ChronologyReason.TIMESTAMP_INVALID)
    if any(
        value is not None and (value.tzinfo is None or value.utcoffset() is None)
        for value in (
            chronology_input.projection_generated_at,
            chronology_input.evidence_observed_at,
            chronology_input.evidence_published_at,
            chronology_input.evidence_processed_at,
        )
    ):
        reasons.append(ChronologyReason.TIMESTAMP_INVALID)
    projection_valid = (
        chronology_input.projection_generated_at is None
        or chronology_input.projection_generated_at.tzinfo is not None
        and chronology_input.projection_generated_at.utcoffset() is not None
    )
    evidence_valid = (
        chronology_input.evidence_observed_at is None
        or chronology_input.evidence_observed_at.tzinfo is not None
        and chronology_input.evidence_observed_at.utcoffset() is not None
    )
    if chronology_input.projection_generated_at is None:
        reasons.append(ChronologyReason.PROJECTION_TIMESTAMP_MISSING)
    elif projection_valid and chronology_input.projection_generated_at >= cutoff:
        reasons.append(ChronologyReason.PROJECTION_AT_OR_AFTER_CUTOFF)
    if chronology_input.evidence_observed_at is None or not chronology_input.chronology_proven:
        reasons.append(ChronologyReason.EVIDENCE_CHRONOLOGY_UNPROVEN)
    elif evidence_valid and chronology_input.evidence_observed_at >= cutoff:
        reasons.append(ChronologyReason.EVIDENCE_OBSERVED_AT_OR_AFTER_CUTOFF)
    if (
        chronology_input.evidence_published_at is not None
        and chronology_input.evidence_published_at >= cutoff
    ):
        reasons.append(ChronologyReason.EVIDENCE_PUBLISHED_AT_OR_AFTER_CUTOFF)
    excluded = tuple(dict.fromkeys(reasons))
    status = (
        ChronologyStatus.EXCLUDED_CHRONOLOGY_UNPROVEN
        if any(reason in excluded for reason in (
            ChronologyReason.PROJECTION_TIMESTAMP_MISSING,
            ChronologyReason.EVIDENCE_CHRONOLOGY_UNPROVEN,
            ChronologyReason.TIMESTAMP_INVALID,
        ))
        else ChronologyStatus.EXCLUDED_CHRONOLOGY
        if excluded
        else ChronologyStatus.VALID
    )
    return ChronologyDecision(status=status, reasons=excluded, cutoff=cutoff)


def assess_observation_chronology(
    observation: LineupEvidenceValidationObservation,
    *,
    cutoff: datetime,
) -> ChronologyDecision:
    """Assess a complete #92 observation through the narrow #93 boundary."""

    return assess_chronology_input(ChronologyInput.from_observation(observation), cutoff=cutoff)


def join_lineup_outcomes(
    observations: Sequence[LineupEvidenceValidationObservation],
    outcomes: Mapping[tuple[str, int, UUID], RealisedOutcome],
    *,
    cutoff: datetime,
    outcome_source_reference: str,
    outcome_provider_id: str,
    outcome_snapshot_id: str,
    outcome_retrieved_at: datetime,
    outcome_finalised_at: datetime,
) -> tuple[JoinedLineupOutcome, ...]:
    """Join by exact logical identity and retain missing rows explicitly.

    The outcome mapping must already have passed official-source finality and exact
    FPL-element-to-canonical identity validation in its provider adapter.
    """

    seen: set[tuple[str, int, UUID]] = set()
    joined: list[JoinedLineupOutcome] = []
    for observation in sorted(observations, key=lambda item: item.logical_identity):
        identity = observation.logical_identity
        if identity in seen:
            raise ValueError(f"duplicate observation identity: {identity}")
        seen.add(identity)
        chronology = assess_observation_chronology(observation, cutoff=cutoff)
        outcome = outcomes.get(identity)
        if outcome is None:
            missing = MissingRealisedOutcome(
                season=observation.season,
                gameweek=observation.gameweek,
                canonical_player_id=observation.canonical_player_id,
                source_reference=outcome_source_reference,
                provider_id=outcome_provider_id,
                snapshot_id=outcome_snapshot_id,
                retrieved_at=outcome_retrieved_at,
                finalised_at=outcome_finalised_at,
            )
            state = OutcomeState.MISSING
            record: RealisedOutcome | MissingRealisedOutcome = missing
        else:
            if outcome.logical_identity != identity:
                raise ValueError(f"outcome identity mismatch: expected {identity}")
            state = OutcomeState.STARTED if outcome.started else OutcomeState.NON_START
            record = outcome
        joined.append(
            JoinedLineupOutcome(
                observation=observation,
                chronology=chronology,
                outcome=record,
                outcome_state=state,
            )
        )
    return tuple(joined)


def valid_joined_outcomes(
    joined: Sequence[JoinedLineupOutcome],
) -> tuple[JoinedLineupOutcome, ...]:
    """Return only valid chronology rows, leaving excluded rows available for audit."""

    return tuple(
        item for item in joined if item.chronology.status is ChronologyStatus.VALID
    )
