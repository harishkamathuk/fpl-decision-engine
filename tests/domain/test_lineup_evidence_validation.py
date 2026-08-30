"""Acceptance tests for immutable lineup-evidence domain observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import (
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    Projection,
)

PLAYER_ID = UUID(int=92_001)
GENERATED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 8, 30, 12, 5, tzinfo=UTC)
RETRIEVED_AT = OBSERVED_AT + timedelta(minutes=1)
PROCESSED_AT = RETRIEVED_AT + timedelta(minutes=1)


def make_projection(**updates: object) -> Projection:
    values: dict[str, object] = {
        "player_id": PLAYER_ID,
        "gameweek": GameweekNumber(value=1),
        "expected_points": 7.25,
        "expected_minutes": 83.5,
        "appearance_probability": 0.91,
        "start_probability": 0.4375,
        "variance": 2.0,
        "source": "synthetic-projection",
        "model_version": "model-v1",
        "generated_at": GENERATED_AT,
    }
    values.update(updates)
    return Projection.model_validate(values)


def make_evidence(**updates: object) -> LineupEvidenceProvenance:
    values: dict[str, object] = {
        "provider_id": "synthetic-lineup",
        "provider_version": "evidence-v1",
        "source_reference": "fixture://lineup-evidence.json",
        "snapshot_id": "lineup-snapshot-1",
        "evidence_ids": ("evidence-1", "evidence-2"),
        "raw_sha256": "a" * 64,
        "mapping_fingerprint": "b" * 64,
        "published_at": OBSERVED_AT - timedelta(hours=1),
        "updated_at": OBSERVED_AT - timedelta(minutes=30),
        "observed_at": OBSERVED_AT,
        "retrieved_at": RETRIEVED_AT,
        "processed_at": PROCESSED_AT,
    }
    values.update(updates)
    return LineupEvidenceProvenance.model_validate(values)


def make_observation(
    *,
    status: LineupEvidenceStatus = LineupEvidenceStatus.CLASSIFIED,
    evidence_class: LineupEvidenceClass | None = LineupEvidenceClass.SUPPORTS_START,
    projection: Projection | None = None,
    evidence: LineupEvidenceProvenance | None = None,
) -> LineupEvidenceValidationObservation:
    return LineupEvidenceValidationObservation.from_projection(
        season="2026-27",
        projection=projection or make_projection(),
        projection_provider_version="projection-v1",
        projection_source_reference="fixture://projection.csv",
        projection_source_sha256="c" * 64,
        projection_snapshot_id="projection-snapshot-1",
        projection_mapping_fingerprint="d" * 64,
        evidence_status=status,
        evidence_class=evidence_class,
        evidence=evidence or make_evidence(),
    )


@pytest.mark.parametrize(
    "evidence_class",
    tuple(LineupEvidenceClass),
)
def test_all_approved_classes_are_valid(evidence_class: LineupEvidenceClass) -> None:
    observation = make_observation(evidence_class=evidence_class)

    assert observation.evidence_status is LineupEvidenceStatus.CLASSIFIED
    assert observation.evidence_class is evidence_class


@pytest.mark.parametrize("status", (LineupEvidenceStatus.MISSING, LineupEvidenceStatus.CONFLICTING))
def test_missing_and_conflicting_states_have_no_class(
    status: LineupEvidenceStatus,
) -> None:
    observation = make_observation(status=status, evidence_class=None)

    assert observation.evidence_status is status
    assert observation.evidence_class is None


@pytest.mark.parametrize(
    ("status", "evidence_class"),
    [
        (LineupEvidenceStatus.CLASSIFIED, None),
        (LineupEvidenceStatus.MISSING, LineupEvidenceClass.SUPPORTS_START),
        (LineupEvidenceStatus.CONFLICTING, LineupEvidenceClass.SUPPORTS_BENCH),
    ],
)
def test_illegal_status_class_combinations_are_rejected(
    status: LineupEvidenceStatus,
    evidence_class: LineupEvidenceClass | None,
) -> None:
    with pytest.raises(ValidationError, match="evidence class"):
        make_observation(status=status, evidence_class=evidence_class)


def test_projection_fields_and_identity_are_copied_without_mutating_projection() -> None:
    projection = make_projection()
    before = projection.model_copy(deep=True)

    observation = make_observation(projection=projection)

    assert observation.logical_identity == ("2026-27", 1, PLAYER_ID)
    assert observation.original_p_start == projection.start_probability == 0.4375
    assert observation.projection_generated_at == projection.generated_at
    assert observation.projection_provider_id == projection.source
    assert observation.projection_model_version == projection.model_version
    assert projection == before
    assert observation.evidence.provider_id == "synthetic-lineup"
    assert observation.evidence.provider_version == "evidence-v1"
    assert observation.evidence.source_reference == "fixture://lineup-evidence.json"
    assert observation.evidence.evidence_ids == ("evidence-1", "evidence-2")
    assert observation.evidence.raw_sha256 == "a" * 64
    assert observation.evidence.mapping_fingerprint == "b" * 64


def test_missing_start_probability_is_rejected() -> None:
    with pytest.raises(ValueError, match="start_probability"):
        make_observation(projection=make_projection(start_probability=None))


def test_missing_publication_time_remains_missing() -> None:
    assert make_evidence(published_at=None).published_at is None


def test_naive_and_misordered_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_evidence(observed_at=datetime(2026, 8, 30, 12, 5))
    with pytest.raises(ValidationError, match="updated_at"):
        make_evidence(updated_at=OBSERVED_AT - timedelta(hours=2))
    with pytest.raises(ValidationError, match="retrieved_at"):
        make_evidence(retrieved_at=OBSERVED_AT - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="processed_at"):
        make_evidence(processed_at=OBSERVED_AT)


def test_non_utc_offset_is_aware_and_semantically_preserved() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    observed = OBSERVED_AT.astimezone(offset)
    evidence = make_evidence(
        observed_at=observed,
        retrieved_at=observed + timedelta(minutes=1),
        processed_at=observed + timedelta(minutes=2),
    )

    assert evidence.observed_at == observed
    assert evidence.observed_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_observation_is_immutable() -> None:
    observation = make_observation()

    with pytest.raises(ValidationError, match="frozen"):
        observation.original_p_start = 0.1
