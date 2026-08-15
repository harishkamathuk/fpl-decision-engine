from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fpl_decision_engine.application import (
    apply_availability_exclusions,
    assess_availability,
)
from fpl_decision_engine.domain import (
    AvailabilityDisposition,
    AvailabilityEvidence,
    AvailabilityReason,
    AvailabilityState,
    EvidenceConfidence,
    EvidenceTemporalRelation,
    GameweekNumber,
    Money,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
)
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser

GENERATED_AT = datetime(2026, 8, 11, 4, 53, tzinfo=UTC)
DEFAULT_PLAYER_ID = UUID(int=1)


def projection(player_id: UUID = DEFAULT_PLAYER_ID) -> Projection:
    return Projection(
        player_id=player_id,
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        appearance_probability=0.8,
        source="forecast",
        model_version="v1",
        generated_at=GENERATED_AT,
    )


def evidence(
    evidence_id: str,
    *,
    player_id: UUID = DEFAULT_PLAYER_ID,
    published_at: datetime | None = None,
    state: AvailabilityState = AvailabilityState.UNAVAILABLE,
    reason: AvailabilityReason = AvailabilityReason.INJURY,
    confidence: EvidenceConfidence = EvidenceConfidence.DEFINITIVE,
) -> AvailabilityEvidence:
    observed_at = datetime(2026, 8, 15, 10, tzinfo=UTC)
    return AvailabilityEvidence(
        evidence_id=evidence_id,
        player_id=player_id,
        state=state,
        reason=reason,
        confidence=confidence,
        source_provider="synthetic-fpl",
        source_snapshot_id="snapshot",
        source_external_player_id=str(player_id.int),
        published_at=published_at,
        observed_at=observed_at,
        processed_at=observed_at,
    )


def one_assessment(*items: AvailabilityEvidence, stale_before: datetime | None = None):
    return assess_availability((projection(),), items, stale_before=stale_before).assessments[0]


def test_newer_definitive_unavailable_excludes_without_mutating_projection() -> None:
    original = projection()
    item = evidence("new", published_at=GENERATED_AT + timedelta(hours=1))

    result = assess_availability((original,), (item,))

    assert result.excluded_player_ids == frozenset({original.player_id})
    assert result.assessments[0].disposition is AvailabilityDisposition.EXCLUDE
    assert original.expected_points == 7.5
    assert original.appearance_probability == 0.8


@pytest.mark.parametrize(
    "state,reason,confidence,expected",
    [
        (
            AvailabilityState.DOUBTFUL,
            AvailabilityReason.DOUBTFUL,
            EvidenceConfidence.AMBIGUOUS,
            AvailabilityDisposition.REVIEW,
        ),
        (
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.INJURY,
            EvidenceConfidence.INDICATIVE,
            AvailabilityDisposition.REVIEW,
        ),
        (
            AvailabilityState.AVAILABLE,
            AvailabilityReason.AVAILABLE,
            EvidenceConfidence.INDICATIVE,
            AvailabilityDisposition.NO_ACTION,
        ),
    ],
)
def test_doubtful_ambiguous_and_return_evidence_never_adjust_points(
    state: AvailabilityState,
    reason: AvailabilityReason,
    confidence: EvidenceConfidence,
    expected: AvailabilityDisposition,
) -> None:
    base = projection()
    assessment = one_assessment(
        evidence(
            "latest",
            published_at=GENERATED_AT + timedelta(hours=1),
            state=state,
            reason=reason,
            confidence=confidence,
        )
    )

    assert assessment.disposition is expected
    assert base.expected_points == 7.5


@pytest.mark.parametrize("offset", [-1, 0])
def test_evidence_known_when_forecast_was_generated_is_not_applied_again(
    offset: int,
) -> None:
    item = evidence("known", published_at=GENERATED_AT + timedelta(seconds=offset))
    assessment = one_assessment(item)

    assert assessment.disposition is AvailabilityDisposition.NO_ACTION
    assert assessment.already_known_evidence_ids == ("known",)
    expected_relation = (
        EvidenceTemporalRelation.OLDER if offset < 0 else EvidenceTemporalRelation.SAME_TIME
    )
    assert assessment.evidence_timing[0].relation is expected_relation


def test_missing_source_time_is_visible_for_review_but_cannot_exclude() -> None:
    assessment = one_assessment(evidence("unknown-time"))

    assert assessment.disposition is AvailabilityDisposition.REVIEW
    assert assessment.unknown_time_evidence_ids == ("unknown-time",)
    assert assessment.evidence_timing[0].relation is EvidenceTemporalRelation.UNKNOWN


def test_stale_evidence_is_retained_without_decision_effect() -> None:
    item = evidence("stale", published_at=GENERATED_AT + timedelta(hours=1))
    assessment = one_assessment(item, stale_before=GENERATED_AT + timedelta(hours=2))

    assert assessment.disposition is AvailabilityDisposition.NO_ACTION
    assert assessment.evidence == (item,)
    assert assessment.stale_evidence_ids == ("stale",)
    assert assessment.evidence_timing[0].relation is EvidenceTemporalRelation.NEWER


def test_newer_evidence_supersedes_older_post_forecast_evidence() -> None:
    unavailable = evidence("first", published_at=GENERATED_AT + timedelta(hours=1))
    available = evidence(
        "return",
        published_at=GENERATED_AT + timedelta(hours=2),
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
    )

    assessment = one_assessment(unavailable, available)

    assert assessment.disposition is AvailabilityDisposition.NO_ACTION
    assert assessment.applied_evidence_ids == ("return",)
    assert assessment.superseded_evidence_ids == ("first",)


def test_contradictory_current_evidence_remains_a_conflict() -> None:
    timestamp = GENERATED_AT + timedelta(hours=1)
    unavailable = evidence("out", published_at=timestamp)
    available = evidence(
        "available",
        published_at=timestamp,
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
    )

    assessment = one_assessment(unavailable, available)

    assert assessment.disposition is AvailabilityDisposition.CONFLICT
    assert not assessment.excluded


def test_assessment_is_independent_of_input_order() -> None:
    items = (
        evidence("first", published_at=GENERATED_AT + timedelta(hours=1)),
        evidence(
            "second",
            published_at=GENERATED_AT + timedelta(hours=2),
            state=AvailabilityState.DOUBTFUL,
            reason=AvailabilityReason.DOUBTFUL,
            confidence=EvidenceConfidence.AMBIGUOUS,
        ),
    )

    assert assess_availability((projection(),), items) == assess_availability(
        (projection(),), tuple(reversed(items))
    )


def test_duplicate_or_unknown_evidence_identity_is_rejected() -> None:
    item = evidence("duplicate", published_at=GENERATED_AT + timedelta(hours=1))
    with pytest.raises(ValueError, match="IDs must be unique"):
        assess_availability((projection(),), (item, item))
    with pytest.raises(ValueError, match="unknown player"):
        assess_availability(
            (projection(),),
            (evidence("unknown", player_id=UUID(int=99), published_at=GENERATED_AT),),
        )


def make_optimisation_request() -> SingleGameweekOptimisationRequest:
    counts = {
        Position.GOALKEEPER: 3,
        Position.DEFENDER: 7,
        Position.MIDFIELDER: 7,
        Position.FORWARD: 5,
    }
    players: list[Player] = []
    projections: list[Projection] = []
    number = 1
    for position in Position:
        for index in range(counts[position]):
            player_id = UUID(int=number)
            players.append(
                Player(
                    id=player_id,
                    team_id=UUID(int=10_000 + ((number - 1) % 8)),
                    first_name=f"First{number}",
                    last_name=f"Last{number}",
                    web_name=f"P{number}",
                    position=position,
                    price=Money(tenths_million=40 + index),
                )
            )
            projections.append(
                projection(player_id).model_copy(update={"expected_points": 30 - number})
            )
            number += 1
    return SingleGameweekOptimisationRequest(
        target_gameweek=GameweekNumber(value=1),
        players=tuple(players),
        projections=tuple(projections),
    )


def test_evidence_exclusion_drives_legal_alternative_without_changing_optimiser() -> None:
    request = make_optimisation_request()
    baseline = HighsSingleGameweekOptimiser().optimise(request)
    selected = baseline.captain_id
    source_projection = next(item for item in request.projections if item.player_id == selected)
    assessments = assess_availability(
        request.projections,
        (
            evidence(
                "definite-out",
                player_id=selected,
                published_at=source_projection.generated_at + timedelta(hours=1),
            ),
        ),
    )

    updated = apply_availability_exclusions(request, assessments)
    alternative = HighsSingleGameweekOptimiser().optimise(updated)

    assert selected not in {member.player_id for member in alternative.squad.members}
    assert len(alternative.squad.members) == 15
    assert request.excluded_players == frozenset()
    assert source_projection == next(
        item for item in request.projections if item.player_id == selected
    )
