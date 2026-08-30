"""Acceptance tests for immutable lineup-evidence JSON persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    Projection,
)
from fpl_decision_engine.infrastructure.persistence import (
    FileLineupEvidenceValidationObservationRepository,
    parse_lineup_observation,
    serialize_lineup_observation,
)
from fpl_decision_engine.ports import (
    LineupObservationConflict,
    LineupObservationPersistenceError,
    LineupObservationUnsupportedSchema,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
PLAYER_ID = UUID(int=92_201)


def make_observation(**updates: object) -> LineupEvidenceValidationObservation:
    projection = Projection(
        player_id=PLAYER_ID,
        gameweek=GameweekNumber(value=3),
        expected_points=6.75,
        expected_minutes=88,
        appearance_probability=0.87,
        start_probability=0.53125,
        source="projection-provider",
        model_version="model-v1",
        generated_at=NOW,
    )
    evidence = LineupEvidenceProvenance(
        provider_id="lineup-provider",
        provider_version="lineup-v2",
        source_reference="fixture://lineup.json",
        snapshot_id="snapshot-3",
        evidence_ids=("evidence-3",),
        raw_sha256="a" * 64,
        mapping_fingerprint="b" * 64,
        published_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=30),
        observed_at=NOW,
        retrieved_at=NOW + timedelta(minutes=1),
        processed_at=NOW + timedelta(minutes=2),
    )
    values: dict[str, object] = {
        "season": "2026-27",
        "projection": projection,
        "projection_provider_version": "projection-v3",
        "projection_source_reference": "fixture://projection.csv",
        "projection_source_sha256": "c" * 64,
        "projection_snapshot_id": "projection-snapshot-3",
        "projection_mapping_fingerprint": "d" * 64,
        "evidence_status": LineupEvidenceStatus.CLASSIFIED,
        "evidence_class": LineupEvidenceClass.SUPPORTS_START,
        "evidence": evidence,
    }
    values.update(updates)
    selected_projection = values.pop("projection")
    selected_evidence = values.pop("evidence")
    return LineupEvidenceValidationObservation.from_projection(
        season=values.pop("season"),
        projection=(
            selected_projection if isinstance(selected_projection, Projection) else projection
        ),
        projection_provider_version=values.pop("projection_provider_version"),
        projection_source_reference=values.pop("projection_source_reference"),
        projection_source_sha256=values.pop("projection_source_sha256"),
        projection_snapshot_id=values.pop("projection_snapshot_id"),
        projection_mapping_fingerprint=values.pop("projection_mapping_fingerprint"),
        evidence_status=values.pop("evidence_status"),
        evidence_class=values.pop("evidence_class"),
        evidence=(
            selected_evidence
            if isinstance(selected_evidence, LineupEvidenceProvenance)
            else evidence
        ),
    )


def test_serialization_is_deterministic_and_round_trip_preserves_fields() -> None:
    observation = make_observation()
    serialized = serialize_lineup_observation(observation)

    assert serialized == serialize_lineup_observation(observation)
    assert parse_lineup_observation(serialized) == observation
    assert b'"original_p_start":0.53125' in serialized


def test_save_load_is_idempotent_and_preserves_timezone_offset(tmp_path: Path) -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    observation = make_observation(
        evidence=LineupEvidenceProvenance(
            provider_id="lineup-provider",
            provider_version="lineup-v2",
            source_reference="fixture://lineup.json",
            snapshot_id="snapshot-3",
            evidence_ids=("evidence-3",),
            raw_sha256="a" * 64,
            mapping_fingerprint="b" * 64,
            observed_at=NOW.astimezone(offset),
            retrieved_at=(NOW + timedelta(minutes=1)).astimezone(offset),
            processed_at=(NOW + timedelta(minutes=2)).astimezone(offset),
        )
    )
    repository = FileLineupEvidenceValidationObservationRepository(tmp_path)

    repository.save(observation)
    path = next(tmp_path.rglob("*.json"))
    original_bytes = path.read_bytes()
    repository.save(observation)

    loaded = repository.get("2026-27", 3, str(PLAYER_ID))
    assert loaded == observation
    assert loaded is not None
    assert loaded.evidence.observed_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert path.read_bytes() == original_bytes


def test_conflicting_identity_never_overwrites_existing_content(tmp_path: Path) -> None:
    repository = FileLineupEvidenceValidationObservationRepository(tmp_path)
    original = make_observation()
    repository.save(original)
    path = next(tmp_path.rglob("*.json"))
    original_bytes = path.read_bytes()
    conflicting = make_observation(
        evidence_class=LineupEvidenceClass.SUPPORTS_BENCH,
    )

    with pytest.raises(LineupObservationConflict):
        repository.save(conflicting)
    assert path.read_bytes() == original_bytes


def test_parser_rejects_schema_json_shape_and_malformed_content() -> None:
    serialized = serialize_lineup_observation(make_observation())
    payload = json.loads(serialized)

    payload["schema_version"] = 2
    with pytest.raises(LineupObservationUnsupportedSchema, match="schema_version 2"):
        parse_lineup_observation(json.dumps(payload).encode())
    with pytest.raises(LineupObservationPersistenceError, match="invalid lineup observation"):
        parse_lineup_observation(b"{")
    with pytest.raises(LineupObservationPersistenceError, match="JSON object"):
        parse_lineup_observation(b"[]")
    del payload["evidence"]
    with pytest.raises(LineupObservationPersistenceError):
        parse_lineup_observation(json.dumps(payload).encode())


def test_path_identity_mismatch_and_missing_observation(tmp_path: Path) -> None:
    repository = FileLineupEvidenceValidationObservationRepository(tmp_path)
    observation = make_observation()
    repository.save(observation)
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    payload["canonical_player_id"] = str(UUID(int=92_202))
    path.write_text(json.dumps(payload))

    with pytest.raises(LineupObservationPersistenceError, match="identity"):
        repository.get("2026-27", 3, str(PLAYER_ID))
    assert repository.get("2026-27", 4, str(PLAYER_ID)) is None


def test_modified_stored_content_is_not_silently_accepted(tmp_path: Path) -> None:
    repository = FileLineupEvidenceValidationObservationRepository(tmp_path)
    observation = make_observation()
    repository.save(observation)
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    payload["original_p_start"] = 0.99
    path.write_text(json.dumps(payload))

    loaded = repository.get("2026-27", 3, str(PLAYER_ID))
    assert loaded is not None
    assert loaded.original_p_start == 0.99
    with pytest.raises(LineupObservationConflict):
        repository.save(observation)
    assert path.read_text() == json.dumps(payload)
