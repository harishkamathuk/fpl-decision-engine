"""Acceptance tests for reproducible #95 lineup validation outputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.application import (
    InvalidLineupValidationArtefact,
    build_lineup_validation_artefact,
    canonical_validation_dataset,
    load_lineup_validation_artefact,
    parse_lineup_validation_artefact,
    render_lineup_validation_summary,
    serialize_lineup_validation_artefact,
    write_lineup_validation_artefact,
)
from fpl_decision_engine.domain import (
    ChronologyDecision,
    ChronologyStatus,
    ExternalRef,
    GameweekNumber,
    JoinedLineupOutcome,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    MissingRealisedOutcome,
    OutcomeState,
    Projection,
    RealisedOutcome,
    calculate_dataset_identity,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
CUTOFF = NOW + timedelta(hours=1)


def joined(index: int, *, missing: bool = False) -> JoinedLineupOutcome:
    player = UUID(int=95000 + index)
    projection = Projection(
        player_id=player,
        gameweek=GameweekNumber(value=index % 2 + 1),
        expected_points=5,
        expected_minutes=80,
        appearance_probability=0.9,
        start_probability=0.25 + index / 10,
        source="synthetic-projection",
        model_version="model-v1",
        generated_at=NOW,
    )
    evidence = LineupEvidenceProvenance(
        provider_id="synthetic-evidence",
        provider_version="evidence-v1",
        source_reference="fixture://evidence",
        snapshot_id="evidence-snapshot",
        evidence_ids=(f"evidence-{index}",),
        raw_sha256=f"{index + 1:064x}",
        mapping_fingerprint="b" * 64,
        observed_at=NOW,
        retrieved_at=NOW,
    )
    observation = LineupEvidenceValidationObservation.from_projection(
        season="2026-27",
        projection=projection,
        projection_provider_version="projection-v1",
        projection_source_reference="fixture://projection",
        projection_source_sha256="c" * 64,
        projection_snapshot_id="projection-snapshot",
        projection_mapping_fingerprint="d" * 64,
        evidence_status=(
            LineupEvidenceStatus.MISSING if missing else LineupEvidenceStatus.CLASSIFIED
        ),
        evidence_class=None if missing else LineupEvidenceClass.SUPPORTS_START,
        evidence=evidence,
    )
    outcome = None
    state = OutcomeState.MISSING
    if missing:
        outcome = MissingRealisedOutcome(
            season="2026-27",
            gameweek=projection.gameweek,
            canonical_player_id=player,
            source_reference="fixture://event/live",
            provider_id="fpl",
            provider_version="api-v1",
            snapshot_id="outcome-snapshot",
            retrieved_at=CUTOFF,
            finalised_at=CUTOFF,
        )
    if not missing:
        outcome = RealisedOutcome(
            season="2026-27",
            gameweek=projection.gameweek,
            player_ref=ExternalRef(provider="fpl-element", external_id=str(index)),
            canonical_player_id=player,
            started=index % 2 == 0,
            minutes=90 if index % 2 == 0 else 0,
            source_reference="fixture://event/live",
            provider_id="fpl",
            provider_version="api-v1",
            snapshot_id="outcome-snapshot",
            retrieved_at=CUTOFF,
            finalised_at=CUTOFF,
        )
        state = OutcomeState.STARTED if outcome.started else OutcomeState.NON_START
    return JoinedLineupOutcome(
        observation=observation,
        chronology=ChronologyDecision(status=ChronologyStatus.VALID, cutoff=CUTOFF),
        outcome=outcome,
        outcome_state=state,
    )


def test_dataset_is_complete_sorted_and_reversible() -> None:
    records = [joined(1), joined(2, missing=True), joined(3)]
    first = canonical_validation_dataset(records)
    second = canonical_validation_dataset(list(reversed(records)))

    assert first == second
    assert [row.logical_identity for row in first] == sorted(row.logical_identity for row in first)
    missing = next(row for row in first if row.outcome_state is OutcomeState.MISSING)
    assert missing.outcome_kind.value == "MISSING_OUTCOME"
    assert missing.evidence_status is LineupEvidenceStatus.MISSING
    assert missing.evidence_class is None


def test_dataset_provenance_and_missing_outcome_version_are_retained() -> None:
    artefact = build_lineup_validation_artefact([joined(1), joined(2, missing=True)])
    missing = next(
        row for row in artefact.dataset if row.outcome_kind.value == "MISSING_OUTCOME"
    )
    realised = next(
        row for row in artefact.dataset if row.outcome_state is not OutcomeState.MISSING
    )
    assert realised.projection_source_sha256 == "c" * 64
    assert realised.evidence_raw_sha256 == "2".zfill(64)
    assert realised.chronology_status is ChronologyStatus.VALID
    assert realised.outcome_provider_version == "api-v1"
    assert missing.outcome_provider_id == "fpl"
    assert missing.outcome_provider_version == "api-v1"
    assert missing.outcome_source_reference == "fixture://event/live"
    assert missing.outcome_snapshot_id == "outcome-snapshot"
    assert missing.outcome_retrieved_at == CUTOFF
    assert missing.outcome_finalised_at == CUTOFF


def test_duplicate_logical_rows_are_rejected() -> None:
    record = joined(1)
    with pytest.raises(ValueError, match="duplicate"):
        canonical_validation_dataset([record, record])


def test_missing_outcome_without_provenance_is_rejected() -> None:
    record = joined(1).model_copy(
        update={"outcome": None, "outcome_state": OutcomeState.MISSING}
    )
    with pytest.raises(
        ValueError, match="MISSING_OUTCOME requires MissingRealisedOutcome provenance"
    ):
        canonical_validation_dataset([record])


def test_dataset_identity_includes_schema_and_complete_rows() -> None:
    dataset = canonical_validation_dataset([joined(1)])
    assert calculate_dataset_identity(dataset, 1) == calculate_dataset_identity(dataset, 1)
    assert calculate_dataset_identity(dataset, 1) != calculate_dataset_identity(dataset, 2)
    changed = dataset[0].model_copy(update={"evidence_raw_sha256": "e" * 64})
    assert calculate_dataset_identity(dataset, 1) != calculate_dataset_identity((changed,), 1)


def test_artifact_contains_exact_evaluation_and_distinct_identities() -> None:
    artefact = build_lineup_validation_artefact([joined(1), joined(2)])

    assert artefact.evaluation.analysis_identity == artefact.analysis_identity
    assert artefact.evaluation.input_dataset_identity == artefact.evaluator_input_dataset_identity
    assert artefact.dataset_identity.startswith("sha256:")
    assert artefact.dataset_identity != artefact.evaluator_input_dataset_identity
    assert artefact.evaluation.protocol_version == "09.01"
    assert artefact.evaluation.conclusion.value == "INSUFFICIENT_EVIDENCE"


def test_tampering_is_rejected_on_parse_and_load(tmp_path: Path) -> None:
    artefact = build_lineup_validation_artefact([joined(1), joined(2)])
    content = serialize_lineup_validation_artefact(artefact)
    payload = json.loads(content)
    payload["dataset"][0]["original_p_start"] = 0.99
    with pytest.raises(InvalidLineupValidationArtefact):
        parse_lineup_validation_artefact(json.dumps(payload).encode() + b"\\n")
    output = write_lineup_validation_artefact(artefact, state_root=tmp_path / "state")
    misleading = output.path.with_name("0" * 64 + ".json")
    misleading.write_bytes(content)
    with pytest.raises(InvalidLineupValidationArtefact):
        load_lineup_validation_artefact(reference=str(misleading), sha256=output.sha256)


def test_load_rejects_correct_bytes_in_wrong_season_directory(tmp_path: Path) -> None:
    artefact = build_lineup_validation_artefact([joined(1), joined(2)])
    output = write_lineup_validation_artefact(artefact, state_root=tmp_path / "state")
    wrong_directory = output.path.parent.with_name("season=2027-28")
    wrong_directory.mkdir()
    wrong_path = wrong_directory / output.path.name
    wrong_path.write_bytes(output.path.read_bytes())

    with pytest.raises(InvalidLineupValidationArtefact, match="season directory"):
        load_lineup_validation_artefact(reference=str(wrong_path), sha256=output.sha256)


def test_serialization_and_rendering_are_deterministic(tmp_path: Path) -> None:
    artefact = build_lineup_validation_artefact([joined(1), joined(2, missing=True)])
    first = serialize_lineup_validation_artefact(artefact)
    second = serialize_lineup_validation_artefact(artefact)
    assert first == second
    assert json.loads(first)["schema_version"] == 1
    assert render_lineup_validation_summary(artefact) == render_lineup_validation_summary(artefact)

    output = write_lineup_validation_artefact(artefact, state_root=tmp_path / "state")
    assert output.path.read_bytes() == first
    assert load_lineup_validation_artefact(
        reference=output.reference, sha256=output.sha256
    ) == artefact


def test_changed_retained_value_changes_dataset_identity() -> None:
    first = build_lineup_validation_artefact([joined(1)])
    changed = joined(1).model_copy(
        update={"observation": joined(1).observation.model_copy(update={"original_p_start": 0.99})}
    )
    second = build_lineup_validation_artefact([changed])
    assert first.dataset_identity != second.dataset_identity


def test_renderer_does_not_use_recomputation() -> None:
    artefact = build_lineup_validation_artefact([joined(1), joined(2, missing=True)])
    rendered = render_lineup_validation_summary(artefact)
    assert "retained_evidence_states: CLASSIFIED=1, MISSING=1, CONFLICTING=0" in rendered
    assert "MISSING_OUTCOME" in rendered
    assert "conclusion: INSUFFICIENT_EVIDENCE" in rendered
    assert "recommend" not in rendered.lower()


def test_non_finite_result_values_cannot_be_serialized() -> None:
    artefact = build_lineup_validation_artefact([joined(1)])
    invalid = artefact.evaluation.provider_baseline.model_copy(
        update={"brier_score": float("nan")}
    )
    invalid_evaluation = artefact.evaluation.model_copy(update={"provider_baseline": invalid})
    invalid_artefact = artefact.model_copy(update={"evaluation": invalid_evaluation})
    with pytest.raises((ValueError, RuntimeError)):
        serialize_lineup_validation_artefact(invalid_artefact)
