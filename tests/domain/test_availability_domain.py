from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.application import assess_availability
from fpl_decision_engine.domain import (
    AvailabilityAssessment,
    AvailabilityDisposition,
    AvailabilityEvidence,
    AvailabilityReason,
    AvailabilityState,
    EvidenceAttribute,
    EvidenceConfidence,
    EvidenceTemporalRelation,
    EvidenceTiming,
    GameweekNumber,
    Projection,
)

OBSERVED_AT = datetime(2026, 8, 15, 10, tzinfo=UTC)


def make_evidence(**updates: object) -> AvailabilityEvidence:
    values: dict[str, object] = {
        "evidence_id": "snapshot:player:1",
        "player_id": UUID(int=1),
        "state": AvailabilityState.UNAVAILABLE,
        "reason": AvailabilityReason.INJURY,
        "confidence": EvidenceConfidence.DEFINITIVE,
        "source_provider": "synthetic-fpl",
        "source_snapshot_id": "snapshot",
        "source_external_player_id": "1",
        "published_at": OBSERVED_AT - timedelta(hours=1),
        "observed_at": OBSERVED_AT,
        "processed_at": OBSERVED_AT + timedelta(minutes=1),
    }
    values.update(updates)
    return AvailabilityEvidence.model_validate(values)


def test_availability_evidence_is_immutable_and_preserves_aware_times() -> None:
    evidence = make_evidence()

    assert evidence.published_at is not None
    assert evidence.published_at.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError, match="frozen"):
        evidence.state = AvailabilityState.AVAILABLE


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"published_at": OBSERVED_AT + timedelta(seconds=1)}, "before publication"),
        ({"processed_at": OBSERVED_AT - timedelta(seconds=1)}, "before observation"),
        (
            {
                "attributes": (
                    EvidenceAttribute(name="status", value="i"),
                    EvidenceAttribute(name="status", value="d"),
                )
            },
            "unique names",
        ),
    ],
)
def test_availability_evidence_rejects_invalid_provenance(
    updates: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        make_evidence(**updates)


def test_missing_publication_time_remains_explicitly_missing() -> None:
    assert make_evidence(published_at=None).published_at is None


def make_assessment(**updates: object) -> AvailabilityAssessment:
    evidence = make_evidence()
    values: dict[str, object] = {
        "player_id": evidence.player_id,
        "projection_generated_at": OBSERVED_AT - timedelta(hours=2),
        "evidence": (evidence,),
        "evidence_timing": (
            EvidenceTiming(
                evidence_id=evidence.evidence_id,
                relation=EvidenceTemporalRelation.NEWER,
            ),
        ),
        "disposition": AvailabilityDisposition.EXCLUDE,
        "applied_evidence_ids": (evidence.evidence_id,),
    }
    values.update(updates)
    return AvailabilityAssessment.model_validate(values)


def test_assessment_rejects_evidence_in_two_disposition_categories() -> None:
    with pytest.raises(ValidationError, match="mutually disjoint"):
        make_assessment(
            already_known_evidence_ids=("snapshot:player:1",),
        )


def test_assessment_rejects_uncategorised_evidence() -> None:
    with pytest.raises(ValidationError, match="categorize every evidence ID exactly once"):
        make_assessment(applied_evidence_ids=())


def test_application_assessment_satisfies_category_partition_invariant() -> None:
    evidence = make_evidence()
    projection = Projection(
        player_id=evidence.player_id,
        gameweek=GameweekNumber(value=1),
        expected_points=5.0,
        source="synthetic",
        model_version="v1",
        generated_at=OBSERVED_AT - timedelta(hours=2),
    )

    result = assess_availability((projection,), (evidence,))

    assert result.assessments[0].applied_evidence_ids == (evidence.evidence_id,)
