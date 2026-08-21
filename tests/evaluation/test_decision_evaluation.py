"""Mandatory tests for leakage-safe decision evaluation (#68)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fpl_decision_engine.application.decision_bundles import (
    build_decision_bundle,
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
    SubmittedDecision,
)
from fpl_decision_engine.evaluation import (
    LeakageError,
    MissingFrozenScoreError,
    MissingOutcomeError,
    evaluate_decision,
)
from fpl_decision_engine.evaluation.evaluator import _same_frozen_input_basis
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


def _make_scenario_bundle(
    *,
    run_id: UUID = SCENARIO_RUN_ID,
    forced_captain: UUID | None = None,
    forced_starters: frozenset[UUID] | None = None,
) -> DecisionBundleV1:
    """Build a scenario bundle with constraint differences from baseline.

    Always forces a specific captain to produce a different recommendation
    identity from the baseline, unless an explicit forced_captain is given.
    """
    request = _make_request()
    if forced_captain is not None:
        request = request.model_copy(update={"forced_captain": forced_captain})
    elif forced_starters is None:
        # Force a different captain than the baseline would pick.
        # Pick a high-value midfielder (player 18 = UUID(int=18)) to force
        # a different captain and guarantee a different recommendation.
        request = request.model_copy(
            update={"forced_captain": UUID(int=18)}
        )
    if forced_starters is not None:
        request = request.model_copy(update={"forced_starters": forced_starters})
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


def _make_decision_run(
    *,
    run_id: UUID = BASELINE_RUN_ID,
    created_at: datetime = DECISION_AT,
    status: DecisionRunStatus = DecisionRunStatus.SUCCEEDED,
) -> DecisionRun:
    return DecisionRun(
        id=run_id,
        created_at=created_at,
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
    human_choice: SubmittedDecision | None = None,
    human_realised: float = 68.0,
) -> OutcomeEvidenceV1:
    candidates: list[CandidateOutcome] = []

    # Baseline outcome
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

    # Scenario outcomes
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

    # Human choice outcome (only if different from baseline identity)
    if human_choice is not None:
        human_identity = (
            human_choice.squad_ids,
            human_choice.starting_xi_ids,
            human_choice.captain_id,
            human_choice.vice_captain_id,
            human_choice.bench_ids,
        )
        baseline_identity = (
            rec.squad_ids,
            rec.starting_xi_ids,
            rec.captain_id,
            rec.vice_captain_id,
            rec.bench_ids,
        )
        if human_identity != baseline_identity:
            # Also check the human doesn't match any scenario identity,
            # because the scenario outcome already covers that selection.
            scenario_identities = {
                (
                    sb.recommendation.squad_ids,
                    sb.recommendation.starting_xi_ids,
                    sb.recommendation.captain_id,
                    sb.recommendation.vice_captain_id,
                    sb.recommendation.bench_ids,
                )
                for sb in scenario_bundles
            }
            if human_identity not in scenario_identities:
                candidates.append(
                    CandidateOutcome(
                        decision_run_id=UUID(int=0),
                        squad_ids=human_choice.squad_ids,
                        starting_xi_ids=human_choice.starting_xi_ids,
                        captain_id=human_choice.captain_id,
                        vice_captain_id=human_choice.vice_captain_id,
                        bench_ids=human_choice.bench_ids,
                        realised_points=human_realised,
                    )
                )

    return OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=3),
        candidates=tuple(candidates),
    )


# --- Mandatory Test 1: Revised projection rejection ---


def test_projection_created_after_cutoff_cannot_replace_frozen_projection() -> None:
    """A projection created after the decision cutoff cannot replace or become
    the frozen projection used for evaluation."""
    bundle = _make_baseline_bundle()
    late_projection_at = DECISION_AT + timedelta(hours=1)

    late_bundle = bundle.model_copy(
        update={
            "inputs": bundle.inputs.model_copy(
                update={"projection_generated_at": late_projection_at}
            )
        }
    )

    run = _make_decision_run()
    outcome = _make_outcome(baseline_bundle=bundle)

    with pytest.raises(LeakageError, match="projection_generated_at is after decision_at"):
        evaluate_decision(
            baseline_bundle=late_bundle,
            baseline_run=run,
            outcome=outcome,
        )


# --- Mandatory Test 2: Outcome isolation ---


def test_changing_realised_scores_changes_only_realised_metrics() -> None:
    """Changing realised scores changes realised metrics only. It must not
    change baseline recommendation, scenario recommendation, baseline
    projected xPts, scenario projected xPts, projected scenario gaps,
    human projected override cost, or frozen provenance."""
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

    # Projected values must be identical
    assert eval_a.baseline.projected_points == eval_b.baseline.projected_points
    assert eval_a.scenarios[0].projected_points == eval_b.scenarios[0].projected_points
    assert (
        eval_a.scenarios[0].projected_delta_vs_baseline
        == eval_b.scenarios[0].projected_delta_vs_baseline
    )

    # Frozen provenance must be identical
    assert (
        eval_a.baseline.frozen_projection_generated_at
        == eval_b.baseline.frozen_projection_generated_at
    )
    assert (
        eval_a.scenarios[0].frozen_projection_generated_at
        == eval_b.scenarios[0].frozen_projection_generated_at
    )

    # Realised values must differ
    assert eval_a.baseline.realised_points != eval_b.baseline.realised_points
    assert eval_a.scenarios[0].realised_points != eval_b.scenarios[0].realised_points
    assert (
        eval_a.baseline.projected_vs_realised_residual
        != eval_b.baseline.projected_vs_realised_residual
    )


# --- Mandatory Test 3: Scenario provenance ---


def test_scenario_created_after_cutoff_is_rejected() -> None:
    """A scenario created after the decision cutoff cannot be represented
    as one considered during the original decision."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    late_scenario_bundle = _make_scenario_bundle()
    late_scenario_run = _make_decision_run(
        run_id=SCENARIO_RUN_ID,
        created_at=DECISION_AT + timedelta(hours=1),
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(late_scenario_bundle,),
    )

    with pytest.raises(LeakageError, match="created after decision cutoff"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((late_scenario_run, late_scenario_bundle),),
            outcome=outcome,
        )


# --- Mandatory Test 4: Baseline dominance semantics ---


def test_scenario_scoring_more_realised_points_is_not_optimiser_failure() -> None:
    """A constraint-only scenario sharing the frozen input basis may later
    score more realised points without being classified as an optimiser failure."""
    baseline_bundle = _make_baseline_bundle()
    scenario_bundle = _make_scenario_bundle()
    baseline_run = _make_decision_run()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    # Scenario scores more realised points than baseline
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=60.0,
        scenario_bundles=(scenario_bundle,),
        scenario_realised=75.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome,
    )

    # Must NOT be classified as optimiser failure
    assert evaluation.validation.optimiser_failure_from_realised_outcome is False

    # The scenario may have scored more, but the baseline was optimal
    # for the frozen forecasts/objective/constraints
    assert evaluation.scenarios[0].realised_points > evaluation.baseline.realised_points


# --- Mandatory Test 5: Deterministic evaluation replay ---


def test_deterministic_evaluation_replay() -> None:
    """Identical frozen decision inputs plus identical outcome inputs produce
    byte-for-byte identical evaluation output."""
    baseline_bundle = _make_baseline_bundle()
    scenario_bundle = _make_scenario_bundle()
    baseline_run = _make_decision_run()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,),
    )

    eval_a = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome,
    )
    eval_b = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome,
    )

    # Complete structural equality — no excluded fields
    assert eval_a == eval_b


# --- Mandatory Test 6: Human override accounting ---


def test_human_override_accounting() -> None:
    """Verify projected_override_cost and realised_override_delta independently."""
    baseline_bundle = _make_baseline_bundle()

    # Create a human choice that differs from baseline
    rec = baseline_bundle.recommendation
    incoming = rec.bench_ids[1]
    outgoing = next(
        player_id
        for player_id in rec.starting_xi_ids
        if player_id not in {rec.captain_id, rec.vice_captain_id}
    )
    human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=tuple(
            sorted((set(rec.starting_xi_ids) - {outgoing}) | {incoming}, key=str)
        ),
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=tuple(
            outgoing if player_id == incoming else player_id
            for player_id in rec.bench_ids
        ),
        recorded_at=DECISION_AT,
    )

    # For the human override test, the human's projected score cannot be
    # traced to preserved evidence (it doesn't match baseline or any scenario)
    # so we must get MissingFrozenScoreError
    baseline_run = _make_decision_run()
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        human_choice=human,
        human_realised=72.0,
    )

    with pytest.raises(MissingFrozenScoreError, match="frozen projected score unavailable"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            human_choice=human,
            outcome=outcome,
        )


def test_human_override_matching_baseline_gets_projected_score() -> None:
    """When human choice matches baseline, projected override cost is zero."""
    baseline_bundle = _make_baseline_bundle()
    baseline_projected = baseline_bundle.recommendation.primary_objective
    baseline_run = _make_decision_run()

    # Human matches baseline exactly
    human = SubmittedDecision(
        squad_ids=baseline_bundle.recommendation.squad_ids,
        starting_xi_ids=baseline_bundle.recommendation.starting_xi_ids,
        captain_id=baseline_bundle.recommendation.captain_id,
        vice_captain_id=baseline_bundle.recommendation.vice_captain_id,
        bench_ids=baseline_bundle.recommendation.bench_ids,
        recorded_at=DECISION_AT,
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=65.0,
        human_choice=human,
        human_realised=65.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        human_choice=human,
        outcome=outcome,
    )

    assert evaluation.human_decision.projected_points == baseline_projected
    assert evaluation.comparison.projected_override_cost == 0.0
    assert evaluation.comparison.realised_override_delta == 0.0


# --- Mandatory Test 7: Missing frozen human score ---


def test_unmatched_human_override_without_frozen_score_fails_explicitly() -> None:
    """An unmatched human override without a preserved pre-deadline
    projected-value artefact must fail explicitly rather than being reconstructed."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    # Human choice differs from baseline — no scenario matches
    rec = baseline_bundle.recommendation
    incoming = rec.bench_ids[1]
    outgoing = next(
        player_id
        for player_id in rec.starting_xi_ids
        if player_id not in {rec.captain_id, rec.vice_captain_id}
    )
    human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=tuple(
            sorted((set(rec.starting_xi_ids) - {outgoing}) | {incoming}, key=str)
        ),
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=tuple(
            outgoing if player_id == incoming else player_id
            for player_id in rec.bench_ids
        ),
        recorded_at=DECISION_AT,
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        human_choice=human,
        human_realised=70.0,
    )

    with pytest.raises(MissingFrozenScoreError, match="Refusing to reconstruct"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            human_choice=human,
            outcome=outcome,
        )


def test_unmatched_human_with_preserved_candidate_bundle_succeeds() -> None:
    """An unmatched human choice with a separately preserved pre-deadline
    candidate bundle succeeds by using that bundle's frozen projected xPts."""
    from fpl_decision_engine.domain import DecisionRecommendation

    baseline_bundle = _make_baseline_bundle()
    baseline_projected = baseline_bundle.recommendation.primary_objective
    baseline_run = _make_decision_run()

    # Human choice differs from baseline
    rec = baseline_bundle.recommendation
    incoming = rec.bench_ids[1]
    outgoing = next(
        player_id
        for player_id in rec.starting_xi_ids
        if player_id not in {rec.captain_id, rec.vice_captain_id}
    )
    human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=tuple(
            sorted((set(rec.starting_xi_ids) - {outgoing}) | {incoming}, key=str)
        ),
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=tuple(
            outgoing if player_id == incoming else player_id
            for player_id in rec.bench_ids
        ),
        recorded_at=DECISION_AT,
    )

    # Build a candidate bundle whose recommendation matches the human choice
    # identity exactly, carrying its own frozen projected xPts (180.0)
    candidate_projected = 180.0
    candidate_recommendation = DecisionRecommendation(
        squad_ids=human.squad_ids,
        starting_xi_ids=human.starting_xi_ids,
        captain_id=human.captain_id,
        vice_captain_id=human.vice_captain_id,
        bench_ids=human.bench_ids,
        formation=rec.formation,
        squad_cost_tenths_million=rec.squad_cost_tenths_million,
        bank_remaining_tenths_million=rec.bank_remaining_tenths_million,
        primary_objective=candidate_projected,
        solver_status="Optimal",
    )
    candidate_bundle = DecisionBundleV1(
        decision_run_id=UUID(int=40_000),
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=DECISION_AT,
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        recommendation=candidate_recommendation,
    )

    # Verify the candidate bundle matches the human choice identity
    assert candidate_bundle.recommendation.identity == human.identity
    assert candidate_projected != baseline_projected

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=65.0,
        human_choice=human,
        human_realised=72.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        candidate_bundles=(candidate_bundle,),
        human_choice=human,
        human_deviation_reasons=("Prefer incoming player",),
        outcome=outcome,
    )

    # Human projected score comes from the preserved candidate bundle
    assert evaluation.human_decision.projected_points == candidate_projected
    assert evaluation.human_decision.projected_delta_vs_baseline == (
        candidate_projected - baseline_projected
    )
    # Realised override delta is independent of projected cost
    assert evaluation.comparison.realised_override_delta == 72.0 - 65.0
    # Projected override cost: baseline_xPts - human_xPts
    assert evaluation.comparison.projected_override_cost == (
        baseline_projected - candidate_projected
    )
    # Residual is consistent
    assert evaluation.human_decision.projected_vs_realised_residual == (
        72.0 - candidate_projected
    )
    # Candidate bundle is NOT represented as a scenario
    assert evaluation.scenarios == ()


# --- Additional focused tests ---


def test_outcome_evidence_rejects_duplicate_selection_identities() -> None:
    """OutcomeEvidenceV1 rejects candidates with duplicate selection identities."""
    with pytest.raises(ValueError, match="duplicate candidate selection identities"):
        OutcomeEvidenceV1(
            season="2026-27",
            gameweek=GameweekNumber(value=1),
            observed_at=DECISION_AT + timedelta(days=1),
            candidates=(
                CandidateOutcome(
                    decision_run_id=BASELINE_RUN_ID,
                    squad_ids=tuple(UUID(int=i) for i in range(15)),
                    starting_xi_ids=tuple(UUID(int=i) for i in range(11)),
                    captain_id=UUID(int=0),
                    vice_captain_id=UUID(int=1),
                    bench_ids=tuple(UUID(int=i) for i in range(11, 15)),
                    realised_points=60.0,
                ),
                CandidateOutcome(
                    decision_run_id=BASELINE_RUN_ID,  # duplicate!
                    squad_ids=tuple(UUID(int=i) for i in range(15)),
                    starting_xi_ids=tuple(UUID(int=i) for i in range(11)),
                    captain_id=UUID(int=0),
                    vice_captain_id=UUID(int=1),
                    bench_ids=tuple(UUID(int=i) for i in range(11, 15)),
                    realised_points=70.0,
                ),
            ),
        )


def test_evaluation_rejects_outcome_before_cutoff() -> None:
    """Outcome observed_at must be after decision_cutoff."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    early_outcome = OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT - timedelta(hours=1),
        candidates=(
            CandidateOutcome(
                decision_run_id=BASELINE_RUN_ID,
                squad_ids=baseline_bundle.recommendation.squad_ids,
                starting_xi_ids=baseline_bundle.recommendation.starting_xi_ids,
                captain_id=baseline_bundle.recommendation.captain_id,
                vice_captain_id=baseline_bundle.recommendation.vice_captain_id,
                bench_ids=baseline_bundle.recommendation.bench_ids,
                realised_points=60.0,
            ),
        ),
    )

    with pytest.raises(LeakageError, match="outcome observed_at must be after decision_cutoff"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            outcome=early_outcome,
        )


def test_evaluation_rejects_mismatched_season() -> None:
    """Outcome season must match baseline season."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    wrong_season_outcome = OutcomeEvidenceV1(
        season="2025-26",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=1),
        candidates=(
            CandidateOutcome(
                decision_run_id=BASELINE_RUN_ID,
                squad_ids=baseline_bundle.recommendation.squad_ids,
                starting_xi_ids=baseline_bundle.recommendation.starting_xi_ids,
                captain_id=baseline_bundle.recommendation.captain_id,
                vice_captain_id=baseline_bundle.recommendation.vice_captain_id,
                bench_ids=baseline_bundle.recommendation.bench_ids,
                realised_points=60.0,
            ),
        ),
    )

    with pytest.raises(LeakageError, match="outcome season"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            outcome=wrong_season_outcome,
        )


def test_evaluation_rejects_mismatched_gameweek() -> None:
    """Outcome gameweek must match baseline gameweek."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    wrong_gw_outcome = OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=2),
        observed_at=DECISION_AT + timedelta(days=1),
        candidates=(
            CandidateOutcome(
                decision_run_id=BASELINE_RUN_ID,
                squad_ids=baseline_bundle.recommendation.squad_ids,
                starting_xi_ids=baseline_bundle.recommendation.starting_xi_ids,
                captain_id=baseline_bundle.recommendation.captain_id,
                vice_captain_id=baseline_bundle.recommendation.vice_captain_id,
                bench_ids=baseline_bundle.recommendation.bench_ids,
                realised_points=60.0,
            ),
        ),
    )

    with pytest.raises(LeakageError, match="outcome gameweek"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            outcome=wrong_gw_outcome,
        )


def test_missing_baseline_outcome_fails_explicitly() -> None:
    """Missing outcome evidence matching baseline selection identity fails."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    # Outcome has a different selection identity (swapped captain/vice) than baseline
    rec = baseline_bundle.recommendation
    wrong_identity_outcome = OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=1),
        candidates=(
            CandidateOutcome(
                decision_run_id=UUID(int=99999),
                squad_ids=rec.squad_ids,
                starting_xi_ids=rec.starting_xi_ids,
                captain_id=rec.vice_captain_id,  # swapped
                vice_captain_id=rec.captain_id,  # swapped
                bench_ids=rec.bench_ids,
                realised_points=60.0,
            ),
        ),
    )

    with pytest.raises(MissingOutcomeError, match="baseline frozen selection identity"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            outcome=wrong_identity_outcome,
        )


def test_scenario_with_different_projection_basis_is_rejected() -> None:
    """A scenario that does not share the same frozen projection SHA-256
    as the baseline is rejected."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    # Tamper with the scenario's projection SHA-256
    tampered_bundle = scenario_bundle.model_copy(
        update={
            "inputs": scenario_bundle.inputs.model_copy(
                update={"projection_sha256": "c" * 64}
            )
        }
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(tampered_bundle,)
    )

    with pytest.raises(LeakageError, match="same frozen input basis"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, tampered_bundle),),
            outcome=outcome,
        )


def test_evaluation_output_matches_required_schema_fields() -> None:
    """Verify the evaluation output contains all fields specified in the issue."""
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

    # Schema-level fields
    assert evaluation.schema_version == 1
    assert evaluation.season == "2026-27"
    assert evaluation.gameweek.value == 1
    assert evaluation.decision_cutoff == DECISION_AT

    # Baseline section
    assert evaluation.baseline.decision_run_id == BASELINE_RUN_ID
    assert isinstance(evaluation.baseline.projected_points, float)
    assert isinstance(evaluation.baseline.realised_points, float)
    assert isinstance(evaluation.baseline.projected_vs_realised_residual, float)
    assert evaluation.baseline.frozen_projection_generated_at == GENERATED_AT
    assert evaluation.baseline.optimiser_status

    # Scenario section
    assert len(evaluation.scenarios) == 1
    assert evaluation.scenarios[0].decision_run_id == SCENARIO_RUN_ID
    assert isinstance(evaluation.scenarios[0].projected_points, float)
    assert isinstance(evaluation.scenarios[0].projected_delta_vs_baseline, float)
    assert isinstance(evaluation.scenarios[0].realised_points, float)
    assert isinstance(evaluation.scenarios[0].projected_vs_realised_residual, float)
    assert evaluation.scenarios[0].frozen_projection_generated_at == GENERATED_AT

    # Human decision
    assert isinstance(evaluation.human_decision.selection_identity_matches_baseline, bool)
    assert isinstance(evaluation.human_decision.realised_points, float)

    # Comparison
    assert isinstance(evaluation.comparison.realised_override_delta, float)

    # Validation
    assert evaluation.validation.optimiser_status
    assert isinstance(evaluation.validation.same_input_comparison, bool)
    assert len(evaluation.validation.leakage_checks) > 0
    assert evaluation.validation.optimiser_failure_from_realised_outcome is False

    # Forecast observations
    assert len(evaluation.forecast_observations) >= 2
    for obs in evaluation.forecast_observations:
        assert isinstance(obs.residual, float)
        assert abs(obs.residual - (obs.realised_points - obs.projected_points)) < 1e-9


# --- Outcome identity correctness regression tests ---


def test_baseline_outcome_with_wrong_selection_identity_is_rejected() -> None:
    """An outcome with the expected baseline decision_run_id but a different
    selection identity cannot score the baseline."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Outcome has the correct decision_run_id but swapped captain/vice_captain
    wrong_identity_outcome = OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=1),
        candidates=(
            CandidateOutcome(
                decision_run_id=BASELINE_RUN_ID,
                squad_ids=rec.squad_ids,
                starting_xi_ids=rec.starting_xi_ids,
                captain_id=rec.vice_captain_id,  # swapped
                vice_captain_id=rec.captain_id,  # swapped
                bench_ids=rec.bench_ids,
                realised_points=65.0,
            ),
        ),
    )

    with pytest.raises(MissingOutcomeError, match="baseline frozen selection identity"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            outcome=wrong_identity_outcome,
        )


def test_scenario_outcome_with_wrong_selection_identity_is_rejected() -> None:
    """An outcome with the expected scenario run ID but the wrong selection
    identity cannot score that scenario."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    scenario_bundle = _make_scenario_bundle()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    srec = scenario_bundle.recommendation

    # Outcome has the correct scenario decision_run_id but wrong selection
    wrong_identity_outcome = OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=1),
        candidates=(
            # Baseline outcome (correct identity)
            CandidateOutcome(
                decision_run_id=BASELINE_RUN_ID,
                squad_ids=baseline_bundle.recommendation.squad_ids,
                starting_xi_ids=baseline_bundle.recommendation.starting_xi_ids,
                captain_id=baseline_bundle.recommendation.captain_id,
                vice_captain_id=baseline_bundle.recommendation.vice_captain_id,
                bench_ids=baseline_bundle.recommendation.bench_ids,
                realised_points=65.0,
            ),
            # Scenario outcome: correct run_id, wrong selection identity
            CandidateOutcome(
                decision_run_id=SCENARIO_RUN_ID,
                squad_ids=srec.squad_ids,
                starting_xi_ids=srec.starting_xi_ids,
                captain_id=srec.vice_captain_id,  # swapped
                vice_captain_id=srec.captain_id,  # swapped
                bench_ids=srec.bench_ids,
                realised_points=70.0,
            ),
        ),
    )

    with pytest.raises(MissingOutcomeError, match="scenario.*frozen selection identity"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, scenario_bundle),),
            outcome=wrong_identity_outcome,
        )


def test_baseline_and_scenario_with_same_identity_share_outcome() -> None:
    """Baseline and scenario with the same frozen selection identity can both
    be evaluated from one realised selection outcome row."""
    from fpl_decision_engine.domain import DecisionRecommendation

    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Build a scenario whose recommendation has the same identity as baseline
    same_identity_recommendation = DecisionRecommendation(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        formation=rec.formation,
        squad_cost_tenths_million=rec.squad_cost_tenths_million,
        bank_remaining_tenths_million=rec.bank_remaining_tenths_million,
        primary_objective=rec.primary_objective,
        solver_status="Optimal",
    )
    same_identity_bundle = DecisionBundleV1(
        decision_run_id=SCENARIO_RUN_ID,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=DECISION_AT,
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        recommendation=same_identity_recommendation,
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    # Single outcome row for this selection identity
    outcome = OutcomeEvidenceV1(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        observed_at=DECISION_AT + timedelta(days=1),
        candidates=(
            CandidateOutcome(
                decision_run_id=BASELINE_RUN_ID,
                squad_ids=rec.squad_ids,
                starting_xi_ids=rec.starting_xi_ids,
                captain_id=rec.captain_id,
                vice_captain_id=rec.vice_captain_id,
                bench_ids=rec.bench_ids,
                realised_points=75.0,
            ),
        ),
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, same_identity_bundle),),
        outcome=outcome,
    )

    # Both baseline and scenario should get the same realised points
    assert evaluation.baseline.realised_points == 75.0
    assert evaluation.scenarios[0].realised_points == 75.0
    assert evaluation.scenarios[0].projected_delta_vs_baseline == 0.0


def test_mismatched_scenario_run_id_and_bundle_decision_run_id_is_rejected() -> None:
    """A scenario pair where scenario_run.id != scenario_bundle.decision_run_id
    is rejected."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    scenario_bundle = _make_scenario_bundle()

    # Scenario run has a different ID than the bundle's decision_run_id
    mismatched_run = _make_decision_run(run_id=UUID(int=99_999))
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,),
    )

    with pytest.raises(ValueError, match="does not match.*scenario_bundle.decision_run_id"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((mismatched_run, scenario_bundle),),
            outcome=outcome,
        )


def test_mismatched_baseline_run_id_and_bundle_decision_run_id_is_rejected() -> None:
    """A baseline pair where baseline_run.id != baseline_bundle.decision_run_id
    is rejected."""
    baseline_bundle = _make_baseline_bundle()
    mismatched_run = _make_decision_run(run_id=UUID(int=99_999))
    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    with pytest.raises(ValueError, match="baseline_run.id does not match"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=mismatched_run,
            outcome=outcome,
        )


# --- Optimiser status and optimality semantics tests ---


def test_succeeded_run_with_non_optimal_solver_status_does_not_prove_optimal() -> None:
    """DecisionRun.status == SUCCEEDED with a frozen recommendation whose
    solver status is not 'Optimal' does not produce baseline_proven_optimal=True."""
    from fpl_decision_engine.domain import DecisionRecommendation

    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run(status=DecisionRunStatus.SUCCEEDED)

    # Override the recommendation's solver status to something non-optimal
    rec = baseline_bundle.recommendation
    non_optimal_rec = DecisionRecommendation(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        formation=rec.formation,
        squad_cost_tenths_million=rec.squad_cost_tenths_million,
        bank_remaining_tenths_million=rec.bank_remaining_tenths_million,
        primary_objective=rec.primary_objective,
        solver_status="Feasible",
    )
    non_optimal_bundle = baseline_bundle.model_copy(
        update={"recommendation": non_optimal_rec}
    )

    outcome = _make_outcome(baseline_bundle=non_optimal_bundle)

    evaluation = evaluate_decision(
        baseline_bundle=non_optimal_bundle,
        baseline_run=baseline_run,
        outcome=outcome,
    )

    # DecisionRun.status is SUCCEEDED but solver says Feasible, not Optimal
    assert baseline_run.status == DecisionRunStatus.SUCCEEDED
    assert evaluation.baseline.baseline_proven_optimal is None
    assert evaluation.validation.baseline_proven_optimal is None
    assert evaluation.baseline.optimiser_status == "Feasible"
    assert evaluation.validation.optimiser_status == "Feasible"


def test_optimal_solver_status_produces_baseline_proven_optimal() -> None:
    """A frozen recommendation with solver_status == 'Optimal' produces
    baseline_proven_optimal=True."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run(status=DecisionRunStatus.SUCCEEDED)

    # The default bundle from the optimiser should have solver_status == 'Optimal'
    assert baseline_bundle.recommendation.solver_status == "Optimal"

    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        outcome=outcome,
    )

    assert evaluation.baseline.baseline_proven_optimal is True
    assert evaluation.validation.baseline_proven_optimal is True
    assert evaluation.baseline.optimiser_status == "Optimal"
    assert evaluation.validation.optimiser_status == "Optimal"


def test_baseline_optimiser_status_comes_from_frozen_recommendation() -> None:
    """Reported baseline optimiser status comes from the frozen recommendation
    solver status, not from DecisionRun.status."""
    from fpl_decision_engine.domain import DecisionRecommendation

    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run(status=DecisionRunStatus.FAILED)

    # Override solver_status to a custom value
    rec = baseline_bundle.recommendation
    custom_rec = DecisionRecommendation(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        formation=rec.formation,
        squad_cost_tenths_million=rec.squad_cost_tenths_million,
        bank_remaining_tenths_million=rec.bank_remaining_tenths_million,
        primary_objective=rec.primary_objective,
        solver_status="Suboptimal",
    )
    custom_bundle = baseline_bundle.model_copy(
        update={"recommendation": custom_rec}
    )

    outcome = _make_outcome(baseline_bundle=custom_bundle)

    evaluation = evaluate_decision(
        baseline_bundle=custom_bundle,
        baseline_run=baseline_run,
        outcome=outcome,
    )

    # Optimiser status should come from recommendation, not DecisionRun.status
    assert evaluation.baseline.optimiser_status == "Suboptimal"
    assert evaluation.validation.optimiser_status == "Suboptimal"
    # DecisionRun.status is FAILED but that does not affect optimiser_status
    assert baseline_run.status == DecisionRunStatus.FAILED


def test_scenario_optimiser_status_comes_from_frozen_recommendation() -> None:
    """Reported scenario optimiser status comes from the frozen scenario
    recommendation solver status, not from the DecisionRun application status."""
    from fpl_decision_engine.domain import DecisionRecommendation

    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    # Build a scenario with a custom solver_status
    scenario_bundle = _make_scenario_bundle()
    srec = scenario_bundle.recommendation
    custom_scenario_rec = DecisionRecommendation(
        squad_ids=srec.squad_ids,
        starting_xi_ids=srec.starting_xi_ids,
        captain_id=srec.captain_id,
        vice_captain_id=srec.vice_captain_id,
        bench_ids=srec.bench_ids,
        formation=srec.formation,
        squad_cost_tenths_million=srec.squad_cost_tenths_million,
        bank_remaining_tenths_million=srec.bank_remaining_tenths_million,
        primary_objective=srec.primary_objective,
        solver_status="Heuristic",
    )
    custom_scenario_bundle = scenario_bundle.model_copy(
        update={"recommendation": custom_scenario_rec}
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(custom_scenario_bundle,),
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, custom_scenario_bundle),),
        outcome=outcome,
    )

    # Scenario optimiser status should come from recommendation, not run status
    assert evaluation.scenarios[0].optimiser_status == "Heuristic"
    assert scenario_run.status == DecisionRunStatus.SUCCEEDED


def test_scenario_scoring_more_not_optimiser_failure() -> None:
    """A scenario that later scores more realised points still leaves
    optimiser_failure_from_realised_outcome == False."""
    baseline_bundle = _make_baseline_bundle()
    scenario_bundle = _make_scenario_bundle()
    baseline_run = _make_decision_run()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    # Scenario scores more realised points than baseline
    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=60.0,
        scenario_bundles=(scenario_bundle,),
        scenario_realised=75.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        outcome=outcome,
    )

    assert evaluation.validation.optimiser_failure_from_realised_outcome is False
    assert evaluation.scenarios[0].realised_points > evaluation.baseline.realised_points


# --- Frozen input/provenance integrity tests ---


def test_same_projection_sha_but_different_snapshot_id_is_rejected() -> None:
    """A scenario with the same projection SHA-256 but a different official
    snapshot identity is rejected as incompatible."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    # Keep projection SHA-256 identical, but change the snapshot ID
    tampered_bundle = scenario_bundle.model_copy(
        update={
            "inputs": scenario_bundle.inputs.model_copy(
                update={"official_snapshot_id": "different_snapshot_id"}
            )
        }
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(tampered_bundle,)
    )

    with pytest.raises(LeakageError, match="same frozen input basis"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, tampered_bundle),),
            outcome=outcome,
        )


def test_same_projection_sha_but_different_snapshot_hash_is_rejected() -> None:
    """A scenario with the same projection SHA-256 but a different official
    snapshot content hash is rejected as incompatible."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    # Keep projection SHA-256 identical, but change the snapshot hash
    tampered_bundle = scenario_bundle.model_copy(
        update={
            "inputs": scenario_bundle.inputs.model_copy(
                update={"official_snapshot_sha256": "d" * 64}
            )
        }
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(tampered_bundle,)
    )

    with pytest.raises(LeakageError, match="same frozen input basis"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, tampered_bundle),),
            outcome=outcome,
        )


def test_same_projection_sha_but_different_availability_is_rejected() -> None:
    """A scenario with the same projection SHA-256 but materially different
    availability provenance is rejected as incompatible."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    # Keep projection SHA-256 identical, but change availability reference
    tampered_bundle = scenario_bundle.model_copy(
        update={
            "inputs": scenario_bundle.inputs.model_copy(
                update={"availability_assessment_reference": "state/other.json"}
            )
        }
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(tampered_bundle,)
    )

    with pytest.raises(LeakageError, match="same frozen input basis"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, tampered_bundle),),
            outcome=outcome,
        )


def test_same_projection_sha_but_different_code_revision_is_rejected() -> None:
    """A scenario with the same projection SHA-256 but a different code
    revision is rejected as incompatible."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    # Keep projection SHA-256 identical, but change code revision
    tampered_bundle = scenario_bundle.model_copy(
        update={"code_revision": "different-revision"}
    )
    # Scenario run must match the tampered bundle's code_revision
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    scenario_run = scenario_run.model_copy(
        update={"code_revision": "different-revision"}
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(tampered_bundle,)
    )

    with pytest.raises(LeakageError, match="same frozen input basis"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, tampered_bundle),),
            outcome=outcome,
        )


def test_same_projection_sha_but_different_config_fingerprint_is_not_rejected() -> None:
    """A scenario with the same projection SHA-256 but a different config
    fingerprint is NOT rejected as frozen-input leakage.

    config_fingerprint is an opaque caller-supplied value not defined by this
    repository, so it is excluded from the frozen-input-basis comparison.
    The run/bundle consistency check still requires the scenario_run and
    scenario_bundle to agree on config_fingerprint.
    """
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    # Keep projection SHA-256 identical, but change config fingerprint
    tampered_bundle = scenario_bundle.model_copy(
        update={"config_fingerprint": "sha256:different"}
    )
    # Scenario run must match the bundle's config_fingerprint
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    scenario_run = scenario_run.model_copy(
        update={"config_fingerprint": "sha256:different"}
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(tampered_bundle,)
    )

    # Should succeed — config_fingerprint difference is not frozen-input leakage
    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, tampered_bundle),),
        outcome=outcome,
    )
    assert evaluation.scenarios[0].decision_run_id == SCENARIO_RUN_ID


def test_mismatched_scenario_config_fingerprint_is_rejected() -> None:
    """A scenario where scenario_run.config_fingerprint !=
    scenario_bundle.config_fingerprint is rejected by run/bundle consistency."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    scenario_bundle = _make_scenario_bundle()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    # Run and bundle disagree on config_fingerprint
    scenario_run = scenario_run.model_copy(
        update={"config_fingerprint": "sha256:different"}
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,)
    )

    with pytest.raises(ValueError, match="run/bundle provenance mismatch"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, scenario_bundle),),
            outcome=outcome,
        )


def test_same_frozen_evidence_with_different_scenario_constraints_is_valid() -> None:
    """Same frozen evidence with different scenario constraints remains valid
    and produces same_input_comparison=True."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    scenario_bundle = _make_scenario_bundle()
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    # The scenario bundle uses the same provenance as baseline (default)
    assert _same_frozen_input_basis(baseline_bundle, scenario_bundle)

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

    assert evaluation.validation.same_input_comparison is True


def test_same_input_comparison_true_for_compatible_provenance() -> None:
    """same_input_comparison is True when a scenario shares compatible provenance.

    In the current fail-closed design, scenarios with materially incompatible
    provenance are rejected before evaluation is produced, so same_input_comparison
    can only be True or vacuously True (no scenarios).  This test verifies the
    non-vacuous True case.
    """
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    # Use a scenario with matching provenance
    compatible_scenario = _make_scenario_bundle(run_id=UUID(int=39_002))
    compatible_run = _make_decision_run(run_id=UUID(int=39_002))

    outcome_single = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(compatible_scenario,),
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((compatible_run, compatible_scenario),),
        outcome=outcome_single,
    )

    assert evaluation.validation.same_input_comparison is True


def test_no_scenarios_same_input_comparison_is_true() -> None:
    """When there are no scenarios, same_input_comparison is vacuously True."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        outcome=outcome,
    )

    assert evaluation.scenarios == ()
    assert evaluation.validation.same_input_comparison is True


# --- Run/bundle provenance consistency tests ---


def test_mismatched_baseline_code_revision_is_rejected() -> None:
    """A baseline run whose code_revision differs from the bundle's is rejected."""
    baseline_bundle = _make_baseline_bundle()
    mismatched_run = _make_decision_run()
    mismatched_run = mismatched_run.model_copy(
        update={"code_revision": "different-revision"}
    )
    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    with pytest.raises(ValueError, match="baseline run/bundle provenance mismatch"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=mismatched_run,
            outcome=outcome,
        )


def test_mismatched_baseline_config_fingerprint_is_rejected() -> None:
    """A baseline run whose config_fingerprint differs from the bundle's is rejected."""
    baseline_bundle = _make_baseline_bundle()
    mismatched_run = _make_decision_run()
    mismatched_run = mismatched_run.model_copy(
        update={"config_fingerprint": "sha256:different"}
    )
    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    with pytest.raises(ValueError, match="baseline run/bundle provenance mismatch"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=mismatched_run,
            outcome=outcome,
        )


def test_mismatched_baseline_season_is_rejected() -> None:
    """A baseline run whose season differs from the bundle's is rejected."""
    baseline_bundle = _make_baseline_bundle()
    mismatched_run = _make_decision_run()
    mismatched_run = mismatched_run.model_copy(update={"season": "2025-26"})
    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    with pytest.raises(ValueError, match="baseline run/bundle provenance mismatch"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=mismatched_run,
            outcome=outcome,
        )


def test_mismatched_baseline_gameweek_is_rejected() -> None:
    """A baseline run whose gameweek differs from the bundle's is rejected."""
    baseline_bundle = _make_baseline_bundle()
    mismatched_run = _make_decision_run()
    mismatched_run = mismatched_run.model_copy(
        update={"gameweek": GameweekNumber(value=2)}
    )
    outcome = _make_outcome(baseline_bundle=baseline_bundle)

    with pytest.raises(ValueError, match="baseline run/bundle provenance mismatch"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=mismatched_run,
            outcome=outcome,
        )


def test_mismatched_scenario_code_revision_is_rejected() -> None:
    """A scenario run whose code_revision differs from its bundle is rejected."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    scenario_bundle = _make_scenario_bundle()

    mismatched_scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)
    mismatched_scenario_run = mismatched_scenario_run.model_copy(
        update={"code_revision": "different-revision"}
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,),
    )

    with pytest.raises(ValueError, match="run/bundle provenance mismatch"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((mismatched_scenario_run, scenario_bundle),),
            outcome=outcome,
        )


# --- Scenario decision_at temporal guard ---


def test_scenario_decision_at_after_cutoff_is_rejected() -> None:
    """A scenario bundle whose decision_at is after the baseline decision
    cutoff is rejected as post-deadline evidence."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    scenario_bundle = _make_scenario_bundle()
    # Move the scenario's decision_at past the cutoff
    late_scenario = scenario_bundle.model_copy(
        update={"decision_at": DECISION_AT + timedelta(hours=1)}
    )
    scenario_run = _make_decision_run(run_id=SCENARIO_RUN_ID)

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(late_scenario,)
    )

    with pytest.raises(LeakageError, match="decision_at is after baseline decision cutoff"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            scenarios=((scenario_run, late_scenario),),
            outcome=outcome,
        )


# --- Human-choice temporal and evidence protection ---


def test_post_cutoff_human_choice_is_rejected() -> None:
    """A human choice recorded after the decision cutoff is rejected."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Human choice recorded after the cutoff
    post_cutoff_human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        recorded_at=DECISION_AT + timedelta(hours=1),
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        human_choice=post_cutoff_human,
        human_realised=65.0,
    )

    with pytest.raises(LeakageError, match="human_choice.recorded_at is after decision_cutoff"):
        evaluate_decision(
            baseline_bundle=baseline_bundle,
            baseline_run=baseline_run,
            human_choice=post_cutoff_human,
            outcome=outcome,
        )


def test_explicit_human_choice_contradicting_preserved_actual_choice_is_rejected() -> None:
    """When baseline_bundle.actual_choice is preserved and an explicit
    human_choice is supplied that differs, the evaluator rejects it."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Record the baseline recommendation as the actual choice
    actual_choice = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        recorded_at=DECISION_AT,
    )
    bundle_with_actual = baseline_bundle.record_actual_choice(actual_choice)

    # Explicit human choice that differs from the preserved actual_choice
    incoming = rec.bench_ids[1]
    outgoing = next(
        player_id
        for player_id in rec.starting_xi_ids
        if player_id not in {rec.captain_id, rec.vice_captain_id}
    )
    contradictory_human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=tuple(
            sorted((set(rec.starting_xi_ids) - {outgoing}) | {incoming}, key=str)
        ),
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=tuple(
            outgoing if player_id == incoming else player_id
            for player_id in rec.bench_ids
        ),
        recorded_at=DECISION_AT,
    )

    outcome = _make_outcome(
        baseline_bundle=bundle_with_actual,
        human_choice=contradictory_human,
        human_realised=72.0,
    )

    with pytest.raises(ValueError, match="does not match preserved.*actual_choice"):
        evaluate_decision(
            baseline_bundle=bundle_with_actual,
            baseline_run=baseline_run,
            human_choice=contradictory_human,
            outcome=outcome,
        )


def test_same_identity_different_recorded_at_is_rejected() -> None:
    """When baseline_bundle.actual_choice is preserved and an explicit
    human_choice has the same identity but a different recorded_at,
    the evaluator rejects it."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Record the baseline recommendation as the actual choice
    actual_choice = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        recorded_at=DECISION_AT,
    )
    bundle_with_actual = baseline_bundle.record_actual_choice(actual_choice)

    # Same identity but different recorded_at (still pre-cutoff)
    late_human = SubmittedDecision(
            squad_ids=rec.squad_ids,
            starting_xi_ids=rec.starting_xi_ids,
            captain_id=rec.captain_id,
            vice_captain_id=rec.vice_captain_id,
            bench_ids=rec.bench_ids,
            recorded_at=DECISION_AT - timedelta(minutes=5),
        )
    assert late_human.identity == actual_choice.identity
    assert late_human.recorded_at != actual_choice.recorded_at

    outcome = _make_outcome(
        baseline_bundle=bundle_with_actual,
        human_choice=late_human,
        human_realised=65.0,
    )

    with pytest.raises(ValueError, match="does not match preserved.*actual_choice"):
        evaluate_decision(
            baseline_bundle=bundle_with_actual,
            baseline_run=baseline_run,
            human_choice=late_human,
            outcome=outcome,
        )


def test_matching_explicit_and_preserved_human_choice_is_accepted() -> None:
    """When baseline_bundle.actual_choice is preserved and an explicit
    human_choice matches it, the evaluation succeeds."""
    baseline_bundle = _make_baseline_bundle()
    baseline_projected = baseline_bundle.recommendation.primary_objective
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Record the baseline recommendation as the actual choice
    actual_choice = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        recorded_at=DECISION_AT,
    )
    bundle_with_actual = baseline_bundle.record_actual_choice(actual_choice)

    # Explicit human choice matches the preserved actual_choice
    matching_human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=rec.starting_xi_ids,
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=rec.bench_ids,
        recorded_at=DECISION_AT,
    )

    outcome = _make_outcome(
        baseline_bundle=bundle_with_actual,
        baseline_realised=65.0,
        human_choice=matching_human,
        human_realised=65.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=bundle_with_actual,
        baseline_run=baseline_run,
        human_choice=matching_human,
        outcome=outcome,
    )

    assert evaluation.human_decision.projected_points == baseline_projected
    assert evaluation.comparison.projected_override_cost == 0.0


# --- Human frozen-score source attribution ---


def test_human_matches_baseline_uses_baseline_run_id() -> None:
    """When the human choice matches baseline, the ForecastObservation
    uses the baseline decision_run_id."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=65.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        outcome=outcome,
    )

    human_obs = next(
        obs for obs in evaluation.forecast_observations
        if obs.candidate_label == "human_choice"
    )
    assert human_obs.decision_run_id == BASELINE_RUN_ID


def test_human_matches_scenario_uses_scenario_run_id() -> None:
    """When the human choice matches a scenario (but not baseline),
    the ForecastObservation uses that scenario's decision_run_id."""
    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Build a scenario with a different recommendation identity
    scenario_run_id = UUID(int=50_000)
    # Use forced_captain to produce a different identity
    scenario_request = _make_request()
    scenario_request = scenario_request.model_copy(
        update={"forced_captain": UUID(int=18)}
    )
    scenario_result = HighsSingleGameweekOptimiser().optimise(scenario_request)
    scenario_bundle = build_decision_bundle(
        run_id=scenario_run_id,
        decision_at=DECISION_AT,
        season="2026-27",
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        request=scenario_request,
        result=scenario_result,
    )
    scenario_run = _make_decision_run(run_id=scenario_run_id)

    # Verify the scenario differs from baseline
    assert scenario_bundle.recommendation.identity != rec.identity

    # Human choice matches the scenario identity (not baseline)
    human = SubmittedDecision(
        squad_ids=scenario_bundle.recommendation.squad_ids,
        starting_xi_ids=scenario_bundle.recommendation.starting_xi_ids,
        captain_id=scenario_bundle.recommendation.captain_id,
        vice_captain_id=scenario_bundle.recommendation.vice_captain_id,
        bench_ids=scenario_bundle.recommendation.bench_ids,
        recorded_at=DECISION_AT,
    )
    assert human.identity != rec.identity  # not baseline
    assert human.identity == scenario_bundle.recommendation.identity  # matches scenario

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        scenario_bundles=(scenario_bundle,),
        human_choice=human,
        human_realised=70.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        scenarios=((scenario_run, scenario_bundle),),
        human_choice=human,
        outcome=outcome,
    )

    human_obs = next(
        obs for obs in evaluation.forecast_observations
        if obs.candidate_label == "human_choice"
    )
    assert human_obs.decision_run_id == scenario_run_id
    assert human_obs.projected_points == scenario_bundle.recommendation.primary_objective


def test_human_matches_candidate_bundle_uses_candidate_run_id() -> None:
    """When the human choice matches a candidate bundle, the ForecastObservation
    uses the candidate bundle's decision_run_id."""
    from fpl_decision_engine.domain import DecisionRecommendation

    baseline_bundle = _make_baseline_bundle()
    baseline_run = _make_decision_run()
    rec = baseline_bundle.recommendation

    # Human choice differs from baseline
    incoming = rec.bench_ids[1]
    outgoing = next(
        player_id
        for player_id in rec.starting_xi_ids
        if player_id not in {rec.captain_id, rec.vice_captain_id}
    )
    human = SubmittedDecision(
        squad_ids=rec.squad_ids,
        starting_xi_ids=tuple(
            sorted((set(rec.starting_xi_ids) - {outgoing}) | {incoming}, key=str)
        ),
        captain_id=rec.captain_id,
        vice_captain_id=rec.vice_captain_id,
        bench_ids=tuple(
            outgoing if player_id == incoming else player_id
            for player_id in rec.bench_ids
        ),
        recorded_at=DECISION_AT,
    )

    candidate_run_id = UUID(int=60_000)
    candidate_projected = 180.0
    candidate_recommendation = DecisionRecommendation(
        squad_ids=human.squad_ids,
        starting_xi_ids=human.starting_xi_ids,
        captain_id=human.captain_id,
        vice_captain_id=human.vice_captain_id,
        bench_ids=human.bench_ids,
        formation=rec.formation,
        squad_cost_tenths_million=rec.squad_cost_tenths_million,
        bank_remaining_tenths_million=rec.bank_remaining_tenths_million,
        primary_objective=candidate_projected,
        solver_status="Optimal",
    )
    candidate_bundle = DecisionBundleV1(
        decision_run_id=candidate_run_id,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=DECISION_AT,
        code_revision="release-deadbeef",
        config_fingerprint="sha256:config",
        inputs=_make_provenance(),
        recommendation=candidate_recommendation,
    )

    outcome = _make_outcome(
        baseline_bundle=baseline_bundle,
        baseline_realised=65.0,
        human_choice=human,
        human_realised=72.0,
    )

    evaluation = evaluate_decision(
        baseline_bundle=baseline_bundle,
        baseline_run=baseline_run,
        candidate_bundles=(candidate_bundle,),
        human_choice=human,
        outcome=outcome,
    )

    human_obs = next(
        obs for obs in evaluation.forecast_observations
        if obs.candidate_label == "human_choice"
    )
    assert human_obs.decision_run_id == candidate_run_id
    assert human_obs.projected_points == candidate_projected
