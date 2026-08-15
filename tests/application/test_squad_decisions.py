"""Tests for blank-squad DecisionRun capture and explicit decision bundles."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.application import (
    build_decision_bundle,
    persist_squad_decision_run,
    serialize_decision_bundle,
    write_decision_bundle,
)
from fpl_decision_engine.domain import (
    DecisionBundleV1,
    DecisionInputProvenance,
    GameweekNumber,
    Money,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
    SingleGameweekOptimisationResult,
    SubmittedDecision,
)
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser
from fpl_decision_engine.infrastructure.persistence import DuckDbDecisionRunRepository

DECISION_AT = datetime(2026, 8, 15, 16, 30, tzinfo=UTC)
GENERATED_AT = datetime(2026, 8, 11, 4, 53, 55, tzinfo=UTC)
RUN_ID = UUID(int=39_000)
COUNTS = {
    Position.GOALKEEPER: 3,
    Position.DEFENDER: 7,
    Position.MIDFIELDER: 7,
    Position.FORWARD: 5,
}


def make_request(*, reversed_input: bool = False) -> SingleGameweekOptimisationRequest:
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
    if reversed_input:
        players.reverse()
        projections.reverse()
    return SingleGameweekOptimisationRequest(
        target_gameweek=GameweekNumber(value=1),
        players=tuple(players),
        projections=tuple(projections),
    )


def provenance() -> DecisionInputProvenance:
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


def build_bundle(
    *, reversed_input: bool = False
) -> tuple[
    SingleGameweekOptimisationRequest,
    SingleGameweekOptimisationResult,
    DecisionBundleV1,
]:
    request = make_request(reversed_input=reversed_input)
    result = HighsSingleGameweekOptimiser().optimise(request)
    bundle = build_decision_bundle(
        run_id=RUN_ID,
        decision_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=provenance(),
        request=request,
        result=result,
    )
    return request, result, bundle


def submitted_from_recommendation(bundle: DecisionBundleV1) -> SubmittedDecision:
    recommendation = bundle.recommendation
    return SubmittedDecision(
        squad_ids=recommendation.squad_ids,
        starting_xi_ids=recommendation.starting_xi_ids,
        captain_id=recommendation.captain_id,
        vice_captain_id=recommendation.vice_captain_id,
        bench_ids=recommendation.bench_ids,
        recorded_at=DECISION_AT,
    )


def test_blank_squad_decision_run_round_trip_preserves_complete_identity(tmp_path) -> None:
    request, result, bundle = build_bundle()
    artifact = write_decision_bundle(bundle, state_root=tmp_path / "state")
    repository = DuckDbDecisionRunRepository(tmp_path / "state" / "fpl.duckdb")
    run = persist_squad_decision_run(
        repository,
        run_id=RUN_ID,
        created_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        source_is_dirty=False,
        config_fingerprint="sha256:config",
        input_snapshot_references=("fpl:snapshot-b", "fpl:snapshot-a", "fpl:snapshot-a"),
        request=request,
        result=result,
        output_artifact_reference=artifact.reference,
    )

    assert repository.get(RUN_ID) == run
    assert run.input_snapshot_ids == ("fpl:snapshot-a", "fpl:snapshot-b")
    assert run.projection_versions == ("fpl_forecast:phase9_frontend_v1:run-20260811",)
    assert run.optimiser_engine == "highs-single-gameweek-optimiser-v1"
    assert run.strategy_mode == "blank_squad_single_gameweek"
    assert run.objective_mode == "mean_only_xi_plus_captain"
    assert run.output_artifact_references == (artifact.reference,)
    summary = run.diagnostic_summary or ""
    for member in result.squad.members:
        assert str(member.player_id) in summary
    assert f"starting_xi_ids={','.join(map(str, sorted(result.starting_xi, key=str)))}" in summary
    assert f"captain_id={result.captain_id}" in summary
    assert f"vice_captain_id={result.vice_captain_id}" in summary
    assert f"bench_ids={','.join(map(str, result.bench))}" in summary


def test_bundle_bytes_are_deterministic_under_reversed_solver_inputs() -> None:
    _, _, forward = build_bundle()
    _, _, reversed_bundle = build_bundle(reversed_input=True)

    assert forward.recommendation == reversed_bundle.recommendation
    assert serialize_decision_bundle(forward) == serialize_decision_bundle(reversed_bundle)


def test_recording_identical_actual_choice_has_no_false_deviation() -> None:
    _, _, bundle = build_bundle()
    actual = submitted_from_recommendation(bundle)

    recorded = bundle.record_actual_choice(actual)

    assert recorded.recommendation == bundle.recommendation
    assert bundle.actual_choice is None
    assert recorded.actual_choice == actual
    assert recorded.deviation is None


def test_different_actual_choice_preserves_recommendation_and_requires_reason() -> None:
    _, _, bundle = build_bundle()
    recommendation = bundle.recommendation
    incoming = recommendation.bench_ids[1]
    outgoing = next(
        player_id
        for player_id in recommendation.starting_xi_ids
        if player_id not in {recommendation.captain_id, recommendation.vice_captain_id}
    )
    actual = SubmittedDecision(
        squad_ids=recommendation.squad_ids,
        starting_xi_ids=tuple(
            sorted(
                (set(recommendation.starting_xi_ids) - {outgoing}) | {incoming},
                key=str,
            )
        ),
        captain_id=recommendation.captain_id,
        vice_captain_id=recommendation.vice_captain_id,
        bench_ids=tuple(
            outgoing if player_id == incoming else player_id
            for player_id in recommendation.bench_ids
        ),
        recorded_at=DECISION_AT,
    )

    with pytest.raises(ValueError, match="requires at least one deviation reason"):
        bundle.record_actual_choice(actual)
    recorded = bundle.record_actual_choice(
        actual,
        deviation_reasons=("Late confirmed team news",),
    )

    assert recorded.recommendation == recommendation
    assert bundle.actual_choice is None
    assert recorded.actual_choice == actual
    assert recorded.deviation is not None
    assert recorded.deviation.reasons == ("Late confirmed team news",)


def test_schema_version_and_canonical_uuid_order_are_validated() -> None:
    _, _, bundle = build_bundle()
    payload = bundle.model_dump()
    payload["schema_version"] = 2
    with pytest.raises(ValidationError, match="unsupported decision bundle schema_version 2"):
        DecisionBundleV1.model_validate(payload)

    selection = submitted_from_recommendation(bundle).model_dump()
    selection["squad_ids"] = tuple(reversed(selection["squad_ids"]))
    with pytest.raises(ValidationError, match="canonical UUID order"):
        SubmittedDecision.model_validate(selection)


def test_content_addressed_writer_is_repeatable_and_never_overwrites_recommendation(
    tmp_path,
) -> None:
    _, _, bundle = build_bundle()
    first = write_decision_bundle(bundle, state_root=tmp_path / "state")
    repeated = write_decision_bundle(bundle, state_root=tmp_path / "state")
    actual_bundle = bundle.record_actual_choice(submitted_from_recommendation(bundle))
    submitted = write_decision_bundle(actual_bundle, state_root=tmp_path / "state")

    assert first == repeated
    assert first.sha256 == hashlib.sha256(serialize_decision_bundle(bundle)).hexdigest()
    assert first.path.read_bytes() == serialize_decision_bundle(bundle)
    assert submitted.path != first.path
    assert first.path.exists()
    assert json.loads(first.path.read_bytes())["actual_choice"] is None
    assert json.loads(submitted.path.read_bytes())["actual_choice"] is not None


def test_recommendation_bundle_contains_no_realised_or_evaluation_fields() -> None:
    _, _, bundle = build_bundle()
    payload = json.loads(serialize_decision_bundle(bundle))

    assert payload["actual_choice"] is None
    assert payload["deviation"] is None
    assert "realised_points" not in payload
    assert "evaluation" not in payload
    assert "regret" not in payload


def test_recommendation_and_actual_choice_enforce_complete_selection_structure() -> None:
    _, result, bundle = build_bundle()
    actual = submitted_from_recommendation(bundle)

    assert bundle.recommendation.bench_ids == result.bench
    assert actual.bench_ids == result.bench
    for selection in (bundle.recommendation, actual):
        assert len(selection.squad_ids) == len(set(selection.squad_ids)) == 15
        assert len(selection.starting_xi_ids) == len(set(selection.starting_xi_ids)) == 11
        assert len(selection.bench_ids) == len(set(selection.bench_ids)) == 4
        assert set(selection.starting_xi_ids) | set(selection.bench_ids) == set(selection.squad_ids)
        assert not set(selection.starting_xi_ids) & set(selection.bench_ids)
        assert selection.captain_id in selection.starting_xi_ids
        assert selection.vice_captain_id in selection.starting_xi_ids
        assert selection.captain_id != selection.vice_captain_id

        for field_name in ("squad_ids", "starting_xi_ids", "bench_ids"):
            payload = selection.model_dump()
            payload[field_name] = payload[field_name][:-1]
            with pytest.raises(ValidationError):
                type(selection).model_validate(payload)

            payload = selection.model_dump()
            values = list(payload[field_name])
            values[-1] = values[0]
            payload[field_name] = tuple(values)
            with pytest.raises(ValidationError):
                type(selection).model_validate(payload)

        payload = selection.model_dump()
        payload["bench_ids"] = (*payload["bench_ids"][:-1], UUID(int=99_999))
        with pytest.raises(ValidationError, match="partition"):
            type(selection).model_validate(payload)

        payload = selection.model_dump()
        payload["captain_id"] = selection.bench_ids[0]
        with pytest.raises(ValidationError, match="both start"):
            type(selection).model_validate(payload)

        payload = selection.model_dump()
        payload["vice_captain_id"] = selection.bench_ids[0]
        with pytest.raises(ValidationError, match="both start"):
            type(selection).model_validate(payload)

        payload = selection.model_dump()
        payload["vice_captain_id"] = selection.captain_id
        with pytest.raises(ValidationError, match="must differ"):
            type(selection).model_validate(payload)


def test_actual_choice_preserves_supplied_bench_order() -> None:
    _, _, bundle = build_bundle()
    recommendation = bundle.recommendation
    reordered = (
        recommendation.bench_ids[0],
        recommendation.bench_ids[2],
        recommendation.bench_ids[1],
        recommendation.bench_ids[3],
    )
    actual = SubmittedDecision(
        squad_ids=recommendation.squad_ids,
        starting_xi_ids=recommendation.starting_xi_ids,
        captain_id=recommendation.captain_id,
        vice_captain_id=recommendation.vice_captain_id,
        bench_ids=reordered,
        recorded_at=DECISION_AT,
    )

    assert actual.bench_ids == reordered


def differing_actual_choice(bundle: DecisionBundleV1, difference: str) -> SubmittedDecision:
    recommendation = bundle.recommendation
    squad_ids = recommendation.squad_ids
    starting_xi_ids = recommendation.starting_xi_ids
    captain_id = recommendation.captain_id
    vice_captain_id = recommendation.vice_captain_id
    bench_ids = recommendation.bench_ids
    if difference == "squad":
        replaced = bench_ids[-1]
        replacement = UUID(int=99_999)
        squad_ids = tuple(sorted((set(squad_ids) - {replaced}) | {replacement}, key=str))
        bench_ids = (*bench_ids[:-1], replacement)
    elif difference == "starting_xi":
        incoming = bench_ids[1]
        outgoing = next(
            value for value in starting_xi_ids if value not in {captain_id, vice_captain_id}
        )
        starting_xi_ids = tuple(sorted((set(starting_xi_ids) - {outgoing}) | {incoming}, key=str))
        bench_ids = tuple(outgoing if value == incoming else value for value in bench_ids)
    elif difference == "captain":
        captain_id = next(
            value for value in starting_xi_ids if value not in {captain_id, vice_captain_id}
        )
    elif difference == "vice_captain":
        vice_captain_id = next(
            value for value in starting_xi_ids if value not in {captain_id, vice_captain_id}
        )
    elif difference == "bench_order":
        bench_ids = (bench_ids[0], bench_ids[2], bench_ids[1], bench_ids[3])
    else:
        raise AssertionError(f"unsupported test difference {difference}")
    return SubmittedDecision(
        squad_ids=squad_ids,
        starting_xi_ids=starting_xi_ids,
        captain_id=captain_id,
        vice_captain_id=vice_captain_id,
        bench_ids=bench_ids,
        recorded_at=DECISION_AT,
    )


@pytest.mark.parametrize(
    "difference",
    ["squad", "starting_xi", "captain", "vice_captain", "bench_order"],
)
def test_every_decision_relevant_difference_requires_a_reason(difference: str) -> None:
    _, _, bundle = build_bundle()
    actual = differing_actual_choice(bundle, difference)
    original_recommendation = bundle.recommendation

    with pytest.raises(ValueError, match="requires at least one deviation reason"):
        bundle.record_actual_choice(actual)
    recorded = bundle.record_actual_choice(actual, deviation_reasons=(f"Changed {difference}",))

    assert bundle.recommendation == original_recommendation
    assert bundle.actual_choice is None
    assert recorded.recommendation == original_recommendation
    assert recorded.actual_choice == actual
    assert recorded.deviation is not None


@pytest.mark.parametrize(
    ("temporal_field", "message"),
    [
        ("projection_generated_at", "projection_generated_at cannot be after decision_at"),
        ("availability_cutoff_at", "availability_cutoff_at cannot be after decision_at"),
        ("recorded_at", "actual_choice.recorded_at cannot be before decision_at"),
    ],
)
def test_bundle_enforces_point_in_time_boundaries(temporal_field: str, message: str) -> None:
    _, _, bundle = build_bundle()
    payload = bundle.model_dump()
    if temporal_field in {"projection_generated_at", "availability_cutoff_at"}:
        payload["inputs"][temporal_field] = DECISION_AT + timedelta(seconds=1)
    else:
        actual = submitted_from_recommendation(bundle).model_copy(
            update={"recorded_at": DECISION_AT - timedelta(seconds=1)}
        )
        payload["actual_choice"] = actual.model_dump()

    with pytest.raises(ValidationError, match=message):
        DecisionBundleV1.model_validate(payload)


def test_existing_content_addressed_path_with_different_bytes_is_rejected(tmp_path) -> None:
    _, _, bundle = build_bundle()
    artifact = write_decision_bundle(bundle, state_root=tmp_path / "state")
    conflicting_bytes = b"corrupted-local-bytes\n"
    artifact.path.write_bytes(conflicting_bytes)

    with pytest.raises(RuntimeError, match="conflicting bytes"):
        write_decision_bundle(bundle, state_root=tmp_path / "state")

    assert artifact.path.read_bytes() == conflicting_bytes
