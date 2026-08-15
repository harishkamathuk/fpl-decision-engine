"""Assess post-forecast availability evidence without changing projections."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fpl_decision_engine.domain import (
    AvailabilityAssessment,
    AvailabilityAssessmentSet,
    AvailabilityDisposition,
    AvailabilityEvidence,
    AvailabilityState,
    EvidenceConfidence,
    EvidenceTemporalRelation,
    EvidenceTiming,
    Projection,
    SingleGameweekOptimisationRequest,
)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _evidence_key(item: AvailabilityEvidence) -> tuple[datetime, str, str]:
    return item.observed_at, item.source_provider, item.evidence_id


def _assess_player(
    projection: Projection,
    evidence: tuple[AvailabilityEvidence, ...],
    stale_before: datetime | None,
) -> AvailabilityAssessment:
    ordered = tuple(sorted(evidence, key=_evidence_key))
    already_known: list[str] = []
    stale: list[str] = []
    unknown_time: list[str] = []
    applicable: list[AvailabilityEvidence] = []
    timing: list[EvidenceTiming] = []

    for item in ordered:
        if item.published_at is None:
            relation = EvidenceTemporalRelation.UNKNOWN
            unknown_time.append(item.evidence_id)
        elif item.published_at < projection.generated_at:
            relation = EvidenceTemporalRelation.OLDER
            already_known.append(item.evidence_id)
        elif item.published_at == projection.generated_at:
            relation = EvidenceTemporalRelation.SAME_TIME
            already_known.append(item.evidence_id)
        elif stale_before is not None and item.published_at < stale_before:
            relation = EvidenceTemporalRelation.NEWER
            stale.append(item.evidence_id)
        else:
            relation = EvidenceTemporalRelation.NEWER
            applicable.append(item)
        timing.append(EvidenceTiming(evidence_id=item.evidence_id, relation=relation))

    disposition = AvailabilityDisposition.NO_ACTION
    applied: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    reasons: list[str] = []

    if applicable:
        latest_at = max(item.published_at for item in applicable if item.published_at is not None)
        latest = tuple(item for item in applicable if item.published_at == latest_at)
        superseded = tuple(item.evidence_id for item in applicable if item not in latest)
        applied = tuple(item.evidence_id for item in latest)
        statements = {(item.state, item.reason) for item in latest}
        if len(statements) > 1:
            disposition = AvailabilityDisposition.CONFLICT
            reasons.append("contradictory evidence at the latest publication time")
        elif all(
            item.state is AvailabilityState.UNAVAILABLE
            and item.confidence is EvidenceConfidence.DEFINITIVE
            for item in latest
        ):
            disposition = AvailabilityDisposition.EXCLUDE
            reasons.append("newer definitive evidence reports the player unavailable")
        elif any(item.state is not AvailabilityState.AVAILABLE for item in latest):
            disposition = AvailabilityDisposition.REVIEW
            reasons.append("newer non-definitive or ambiguous evidence requires review")
        else:
            reasons.append("newer availability evidence does not increase the base forecast")
    elif unknown_time and any(
        item.evidence_id in unknown_time and item.state is not AvailabilityState.AVAILABLE
        for item in ordered
    ):
        disposition = AvailabilityDisposition.REVIEW
        reasons.append("concerning evidence has no source publication timestamp")

    if already_known:
        reasons.append("evidence known at forecast generation was not applied again")
    if stale:
        reasons.append("stale evidence was retained without decision effect")

    return AvailabilityAssessment(
        player_id=projection.player_id,
        projection_generated_at=projection.generated_at,
        evidence=ordered,
        evidence_timing=tuple(timing),
        disposition=disposition,
        applied_evidence_ids=applied,
        superseded_evidence_ids=superseded,
        already_known_evidence_ids=tuple(already_known),
        stale_evidence_ids=tuple(stale),
        unknown_time_evidence_ids=tuple(unknown_time),
        reasons=tuple(reasons),
    )


def assess_availability(
    projections: Sequence[Projection],
    evidence: Sequence[AvailabilityEvidence],
    *,
    stale_before: datetime | None = None,
) -> AvailabilityAssessmentSet:
    """Resolve evidence as a conservative temporal delta over immutable forecasts.

    Evidence published at or before ``Projection.generated_at`` is treated as already
    incorporated. Missing source publication times are never replaced by observation
    time. Only newer, non-stale, definitive unavailability can create an exclusion;
    the function never changes expected points or probability fields.
    """

    if stale_before is not None:
        _require_aware(stale_before, "stale_before")
    projection_by_player = {item.player_id: item for item in projections}
    if len(projection_by_player) != len(projections):
        raise ValueError("availability assessment requires one projection per player")

    evidence_ids = [item.evidence_id for item in evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("availability evidence IDs must be unique")
    unknown_players = sorted(
        {item.player_id for item in evidence} - projection_by_player.keys(), key=str
    )
    if unknown_players:
        raise ValueError(f"availability evidence references unknown player {unknown_players[0]}")

    grouped: dict[UUID, list[AvailabilityEvidence]] = defaultdict(list)
    for item in evidence:
        grouped[item.player_id].append(item)
    assessments = tuple(
        _assess_player(projection_by_player[player_id], tuple(grouped[player_id]), stale_before)
        for player_id in sorted(projection_by_player, key=str)
    )
    return AvailabilityAssessmentSet(
        assessments=assessments,
        excluded_player_ids=frozenset(item.player_id for item in assessments if item.excluded),
        review_player_ids=frozenset(item.player_id for item in assessments if item.requires_review),
        conflict_player_ids=frozenset(
            item.player_id
            for item in assessments
            if item.disposition is AvailabilityDisposition.CONFLICT
        ),
    )


def apply_availability_exclusions(
    request: SingleGameweekOptimisationRequest,
    assessments: AvailabilityAssessmentSet,
) -> SingleGameweekOptimisationRequest:
    """Copy a #6 request with explicit definitive exclusions; leave #6 unchanged."""

    candidate_ids = {player.id for player in request.players}
    unknown = assessments.excluded_player_ids - candidate_ids
    if unknown:
        raise ValueError(
            f"availability exclusion references non-candidate player {min(unknown, key=str)}"
        )
    return request.model_copy(
        update={"excluded_players": request.excluded_players | assessments.excluded_player_ids}
    )
