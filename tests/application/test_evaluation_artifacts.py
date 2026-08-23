"""Tests for deterministic evaluation serialization and content-addressed persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fpl_decision_engine.application import (
    build_decision_bundle,
    serialize_decision_evaluation,
    write_decision_evaluation,
)
from fpl_decision_engine.domain import (
    DecisionBundleV1,
    DecisionInputProvenance,
    DecisionRun,
    DecisionRunStatus,
    GameweekNumber,
    Money,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
)
from fpl_decision_engine.evaluation import evaluate_decision
from fpl_decision_engine.evaluation.contracts import DecisionEvaluationV1
from fpl_decision_engine.evaluation.outcome import CandidateOutcome, OutcomeEvidenceV1
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser

DECISION_AT = datetime(2026, 8, 15, 16, 30, tzinfo=UTC)
GENERATED_AT = datetime(2026, 8, 11, 4, 53, 55, tzinfo=UTC)
BASELINE_RUN_ID = UUID(int=39_000)
SCENARIO_RUN_ID = UUID(int=39_001)
COUNTS = {
    Position.GOALKEEPER: 3,
    Position.DEFENDER: 7,
    Position.MIDFIELDER: 7,
    Position.FORWARD: 5,
}


def _make_request() -> SingleGameweekOptimisationRequest:
    players: list[Player] = []
    projections: list[Projection] = []
    number = 1
    for position in Position:
        for position_index in range(COUNTS[position]):
            player_id = UUID(int=number)
            players.append(
                Player(
                    id=player_id,
                    team_id=UUID(int=10_000 + ((number - 1) % 8)),
                    first_name=f"First{number}",
                    last_name=f"Last{number}",
                    web_name=f"P{number}",
                    position=position,
                    price=Money(
                        tenths_million={
                            Position.GOALKEEPER: 40,
                            Position.DEFENDER: 45,
                            Position.MIDFIELDER: 55,
                            Position.FORWARD: 60,
                        }[position]
                        + position_index
                    ),
                )
            )
            projections.append(
                Projection(
                    player_id=player_id,
                    gameweek=GameweekNumber(value=1),
                    expected_points=20.0 - number * 0.4,
                    source="fpl_forecast",
                    model_version="phase9_frontend_v1:run-20260811",
                    generated_at=GENERATED_AT,
                )
            )
            number += 1
    return SingleGameweekOptimisationRequest(
        target_gameweek=GameweekNumber(value=1),
        players=tuple(players),
        projections=tuple(projections),
    )


def _make_provenance() -> DecisionInputProvenance:
    return DecisionInputProvenance(
        official_snapshot_reference="data/raw/fpl/2026-27/snapshot/manifest.json",
        official_snapshot_id="20260815T084445Z_c3e7b73647df",
        official_snapshot_sha256="a" * 64,
        projection_provider="fpl_forecast_csv",
        projection_source="fpl_forecast",
        projection_artifact_reference="local/player_gameweek_projections.csv",
        projection_sha256="b" * 64,
        projection_model_version="phase9_frontend_v1:run-20260811",
        projection_generated_at=GENERATED_AT,
        availability_assessment_reference="state/availability/gw1.json",
        availability_cutoff_at=DECISION_AT,
    )


def _make_baseline_bundle() -> DecisionBundleV1:
    request = _make_request()
    result = HighsSingleGameweekOptimiser().optimise(request)
    return build_decision_bundle(
        run_id=BASELINE_RUN_ID,
        decision_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        request=request,
        result=result,
    )


def _make_scenario_bundle() -> DecisionBundleV1:
    request = _make_request()
    request = request.model_copy(update={"forced_captain": UUID(int=18)})
    result = HighsSingleGameweekOptimiser().optimise(request)
    return build_decision_bundle(
        run_id=SCENARIO_RUN_ID,
        decision_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        request=request,
        result=result,
    )


def _make_decision_run(
    *,
    run_id: UUID = BASELINE_RUN_ID,
    status: DecisionRunStatus = DecisionRunStatus.SUCCEEDED,
) -> DecisionRun:
    return DecisionRun(
        id=run_id,
        created_at=DECISION_AT,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        status=status,
        strategy_mode="blank_squad_single_gameweek",
        objective_mode="mean_only_xi_plus_captain",
    )


def _make_outcome(
    *,
    baseline_bundle: DecisionBundleV1,
    baseline_realised: float = 65.0,
    scenario_bundles: tuple[DecisionBundleV1, ...] = (),
    scenario_realised: float = 70.0,
) -> OutcomeEvidenceV1:
    candidates: list[CandidateOutcome] = []

    rec = baseline_bundle.recommendation
    candidates.append(
        CandidateOutcome(
            decision_run_id=baseline_bundle.decision_run_id,
            squad_ids=rec.squad_ids,
            starting_xi_ids=rec.starting_xi_ids,
            captain_id=rec.captain_id,
            vice_captain_id=rec.vice_captain_id,
            bench_ids=rec.bench_ids,
            realised_points=baseline_realised,
        )
    )

    for scenario_bundle in scenario_bundles:
        srec = scenario_bundle.recommendation
        candidates.append(
            CandidateOutcome(
                decision_run_id=scenario_bundle.decision_run_id,
                squad_ids=srec.squad_ids,
                starting_xi_ids=srec.starting_xi_ids,
                captain_id=srec.captain_id,
                vice_captain_id=srec.vice_captain_id,
                bench_ids=srec.bench_ids,
                realised_points=scenario_realised,
            )
        )

    return OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=3),
        candidates=tuple(candidates),
    )


def _build_evaluation() -> DecisionEvaluationV1:
    """Build a complete evaluation from real optimiser output."""
    baseline_bundle = _make_baseline_bundle()
    scenario_bundle = _make_scenario_bundle()
    baseline_run = _make_decision_run()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,),
    )

    return evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome,
    )


# --- Deterministic serialization tests ---


def test_identical_evaluations_serialize_to_identical_bytes() -> None:
    """Identical DecisionEvaluationV1 values serialize to byte-for-byte identical bytes."""
    evaluation = _build_evaluation()

    bytes_a = serialize_decision_evaluation(evaluation)
    bytes_b = serialize_decision_evaluation(evaluation)

    assert bytes_a == bytes_b
    assert len(bytes_a) > 0


def test_serialized_bytes_hash_deterministically() -> None:
    """Serialized bytes hash deterministically via SHA-256."""
    evaluation = _build_evaluation()

    content = serialize_decision_evaluation(evaluation)
    digest = hashlib.sha256(content).hexdigest()

    # Re-serialize and re-hash
    content_again = serialize_decision_evaluation(evaluation)
    digest_again = hashlib.sha256(content_again).hexdigest()

    assert digest == digest_again
    # SHA-256 hex digest is always 64 lowercase hex chars
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_serialized_json_is_valid_canonical_json() -> None:
    """Serialized bytes are valid JSON with sorted keys, compact separators, trailing newline."""
    evaluation = _build_evaluation()

    content = serialize_decision_evaluation(evaluation)
    # Must end with newline
    assert content.endswith(b"\n")

    # Must be valid JSON
    payload = json.loads(content)
    assert isinstance(payload, dict)

    # Top-level keys must be sorted
    keys = list(payload.keys())
    assert keys == sorted(keys)

    # Re-serialize the parsed payload — should match the original bytes
    reparsed = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    assert content == reparsed


def test_serialized_payload_contains_all_required_fields() -> None:
    """Serialized evaluation contains all required schema fields."""
    evaluation = _build_evaluation()
    payload = json.loads(serialize_decision_evaluation(evaluation))

    assert payload["schema_version"] == 1
    assert payload["season"] == "2026-27"
    assert isinstance(payload["gameweek"], int)
    assert isinstance(payload["decision_cutoff"], str)

    # Baseline
    baseline = payload["baseline"]
    assert "decision_run_id" in baseline
    assert "projected_points" in baseline
    assert "realised_points" in baseline
    assert "projected_vs_realised_residual" in baseline
    assert "frozen_projection_generated_at" in baseline
    assert "optimiser_status" in baseline
    assert "baseline_proven_optimal" in baseline

    # Scenarios
    assert isinstance(payload["scenarios"], list)
    assert len(payload["scenarios"]) == 1
    scenario = payload["scenarios"][0]
    assert "scenario_id" in scenario
    assert "projected_delta_vs_baseline" in scenario
    assert "optimiser_settings_summary" in scenario

    # Human decision
    human = payload["human_decision"]
    assert "selection_identity_matches_baseline" in human
    assert "projected_points" in human
    assert "realised_points" in human

    # Comparison
    comparison = payload["comparison"]
    assert "projected_override_cost" in comparison
    assert "realised_override_delta" in comparison

    # Validation
    validation = payload["validation"]
    assert "optimiser_status" in validation
    assert "same_input_comparison" in validation
    assert "leakage_checks" in validation
    assert "optimiser_failure_from_realised_outcome" in validation

    # Forecast observations
    assert isinstance(payload["forecast_observations"], list)
    assert len(payload["forecast_observations"]) >= 2
    for obs in payload["forecast_observations"]:
        assert "candidate_label" in obs
        assert "residual" in obs


def _make_scenario_bundle_forced_captain(
    forced_captain: UUID,
    run_id: UUID,
) -> DecisionBundleV1:
    """Build a scenario bundle with a specific forced captain."""
    request = _make_request()
    request = request.model_copy(update={"forced_captain": forced_captain})
    result = HighsSingleGameweekOptimiser().optimise(request)
    return build_decision_bundle(
        run_id=run_id,
        decision_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        request=request,
        result=result,
    )


def test_scenario_order_is_preserved_in_serialization() -> None:
    """Scenario order from the evaluator is preserved in serialized output.

    Two distinct scenarios are supplied in deliberately non-sorted order.
    The serializer must preserve the evaluation.scenarios tuple order, not
    sort by UUID, scenario ID, projected points, or another field.
    """
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    # Two distinct scenarios with different forced captains → different identities
    scenario_a = _make_scenario_bundle_forced_captain(
        forced_captain=UUID(int=18), run_id=UUID(int=40_001)
    )
    scenario_b = _make_scenario_bundle_forced_captain(
        forced_captain=UUID(int=20), run_id=UUID(int=40_002)
    )
    scenario_run_a = _make_decision_run(run_id=UUID(int=40_001))
    scenario_run_b = _make_decision_run(run_id=UUID(int=40_002))

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_a, scenario_b),
    )

    # Supply in deliberately non-sorted order: B before A
    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run_b, scenario_b), (scenario_run_a, scenario_a)),
        outcome=outcome,
    )

    # Verify the evaluation reflects the supplied order (B then A)
    assert len(evaluation.scenarios) == 2
    eval_ids = [s.decision_run_id for s in evaluation.scenarios]
    assert eval_ids == [UUID(int=40_002), UUID(int=40_001)]

    # Serialize and verify the JSON preserves the same order
    content = serialize_decision_evaluation(evaluation)
    payload = json.loads(content)
    json_ids = [s["decision_run_id"] for s in payload["scenarios"]]
    assert json_ids == [
        str(UUID(int=40_002)),
        str(UUID(int=40_001)),
    ]

    # The test would fail if the serializer sorted by UUID (40_001 < 40_002),
    # by scenario_id, by projected_points, or by any other field.


# --- Frozen input provenance tests ---


def test_frozen_input_provenance_serializes_deterministically() -> None:
    """Identical frozen input provenance serializes to identical bytes."""
    evaluation = _build_evaluation()

    bytes_a = serialize_decision_evaluation(evaluation)
    bytes_b = serialize_decision_evaluation(evaluation)

    assert bytes_a == bytes_b

    payload = json.loads(bytes_a)
    provenance = payload["frozen_input_provenance"]

    # The frozen decision basis is preserved and matches the baseline inputs.
    assert provenance["official_snapshot_id"] == "20260815T084445Z_c3e7b73647df"
    assert provenance["official_snapshot_sha256"] == "a" * 64
    assert provenance["projection_sha256"] == "b" * 64
    assert provenance["projection_model_version"] == "phase9_frontend_v1:run-20260811"
    assert provenance["projection_generated_at"] == (
        GENERATED_AT.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert provenance["availability_assessment_reference"] == (
        "state/availability/gw1.json"
    )
    assert provenance["availability_cutoff_at"] == (
        DECISION_AT.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert provenance["code_revision"] == "release-deadbeef"


def test_changing_projection_sha_changes_serialized_bytes() -> None:
    """A different projection SHA-256 changes the serialized evaluation bytes."""
    evaluation = _build_evaluation()

    # Same evaluation but with a different projection SHA-256 in the inputs.
    inputs = _make_provenance().model_copy(update={"projection_sha256": "c" * 64})
    request = _make_request()
    result = HighsSingleGameweekOptimiser().optimise(request)
    different_bundle = build_decision_bundle(
        run_id=BASELINE_RUN_ID,
        decision_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=inputs,
        request=request,
        result=result,
    )
    different_evaluation = evaluate_decision(
        baseline_bundle=different_bundle,
        baseline_run=_make_decision_run(),
        outcome=_make_outcome(baseline_bundle=different_bundle),
    )

    assert different_evaluation.frozen_input_provenance.projection_sha256 == "c" * 64
    assert serialize_decision_evaluation(
        different_evaluation
    ) != serialize_decision_evaluation(evaluation)


def test_persisted_evaluation_contains_frozen_input_provenance(tmp_path) -> None:
    """The persisted evaluation artefact preserves the frozen input provenance."""
    evaluation = _build_evaluation()

    artifact = write_decision_evaluation(evaluation, state_root=tmp_path / "state")
    persisted = json.loads(artifact.path.read_bytes())

    provenance = persisted["frozen_input_provenance"]
    assert provenance["official_snapshot_id"] == "20260815T084445Z_c3e7b73647df"
    assert provenance["official_snapshot_sha256"] == "a" * 64
    assert provenance["projection_sha256"] == "b" * 64
    assert provenance["projection_model_version"] == "phase9_frontend_v1:run-20260811"
    assert provenance["projection_generated_at"] is not None
    assert provenance["availability_assessment_reference"] == (
        "state/availability/gw1.json"
    )
    assert provenance["availability_cutoff_at"] is not None
    assert provenance["code_revision"] == "release-deadbeef"

    # The provenance is addressable as part of the immutable content hash.
    assert artifact.sha256 == hashlib.sha256(
        serialize_decision_evaluation(evaluation)
    ).hexdigest()


# --- Content-addressed persistence tests ---


def test_write_evaluation_is_idempotent(tmp_path) -> None:
    """Writing the same evaluation twice is idempotent — same artifact returned."""
    evaluation = _build_evaluation()

    first = write_decision_evaluation(evaluation, state_root=tmp_path / "state")
    repeated = write_decision_evaluation(evaluation, state_root=tmp_path / "state")

    assert first == repeated
    assert first.sha256 == hashlib.sha256(
        serialize_decision_evaluation(evaluation)
    ).hexdigest()


def test_persisted_content_matches_serializer_output(tmp_path) -> None:
    """Persisted file content matches the serializer output exactly."""
    evaluation = _build_evaluation()

    artifact = write_decision_evaluation(evaluation, state_root=tmp_path / "state")
    expected_content = serialize_decision_evaluation(evaluation)

    assert artifact.path.read_bytes() == expected_content
    assert artifact.sha256 == hashlib.sha256(expected_content).hexdigest()
    assert artifact.reference == str(artifact.path)


def test_write_evaluation_creates_correct_directory_layout(tmp_path) -> None:
    """Content-addressed path follows the convention:
    state/decision-evaluations/season=YYYY-YY/gameweek=N/<sha256>.json
    """
    evaluation = _build_evaluation()
    artifact = write_decision_evaluation(evaluation, state_root=tmp_path / "state")

    # Verify directory structure
    assert "decision-evaluations" in str(artifact.path)
    assert f"season={evaluation.season}" in str(artifact.path)
    assert f"gameweek={evaluation.gameweek.value}" in str(artifact.path)
    assert artifact.path.suffix == ".json"
    # The filename is the SHA-256 hash
    assert artifact.path.stem == artifact.sha256


def test_conflicting_bytes_at_same_path_is_rejected(tmp_path) -> None:
    """Writing to a path already occupied by different bytes raises RuntimeError."""
    evaluation = _build_evaluation()
    artifact = write_decision_evaluation(evaluation, state_root=tmp_path / "state")

    # Corrupt the file
    artifact.path.write_bytes(b"corrupted\n")

    with pytest.raises(RuntimeError, match="conflicting bytes"):
        write_decision_evaluation(evaluation, state_root=tmp_path / "state")

    # Original corrupted content is preserved (no silent overwrite)
    assert artifact.path.read_bytes() == b"corrupted\n"


def test_different_evaluations_produce_different_hashes(tmp_path) -> None:
    """Different evaluation inputs produce different content hashes."""
    baseline_bundle = _make_baseline_bundle()
    scenario_bundle = _make_scenario_bundle()
    baseline_run = _make_decision_run()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome_a = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=60.0,
        scenario_bundles=(scenario_bundle,),
        scenario_realised=65.0,
    )
    outcome_b = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=80.0,
        scenario_bundles=(scenario_bundle,),
        scenario_realised=90.0,
    )

    eval_a = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome_a,
    )
    eval_b = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome_b,
    )

    artifact_a = write_decision_evaluation(eval_a, state_root=tmp_path / "state")
    artifact_b = write_decision_evaluation(eval_b, state_root=tmp_path / "state")

    assert artifact_a.sha256 != artifact_b.sha256
    assert artifact_a.path != artifact_b.path


def test_forecast_observation_order_is_preserved(tmp_path) -> None:
    """Forecast observation order in the evaluation is preserved in serialized output."""
    baseline_bundle = _make_baseline_bundle()
    scenario_bundle = _make_scenario_bundle()
    baseline_run = _make_decision_run()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,),
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome,
    )

    # Verify the evaluation has observations in expected order
    labels = [obs.candidate_label for obs in evaluation.forecast_observations]
    assert labels[0] == "baseline"
    assert "scenario_" in labels[1]
    assert labels[2] == "human_choice"

    # Serialize and verify order is preserved
    content = serialize_decision_evaluation(evaluation)
    payload = json.loads(content)
    serialized_labels = [
        obs["candidate_label"] for obs in payload["forecast_observations"]
    ]
    assert serialized_labels == labels
