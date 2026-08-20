"""Leakage-safe evaluation of frozen gameweek decisions.

The evaluator is a pure function: no IO, no provider calls, no optimiser
invocation, no forecast generation. It composes already-preserved artefacts
and produces a deterministic evaluation output.
"""

from __future__ import annotations

from fpl_decision_engine.domain import (
    DecisionBundleV1,
    DecisionRun,
    SubmittedDecision,
)

from .contracts import (
    BaselineEvaluation,
    ComparisonSection,
    DecisionEvaluationV1,
    ForecastObservation,
    HumanDecisionEvaluation,
    ScenarioEvaluation,
    ValidationSection,
)
from .outcome import OutcomeEvidenceV1


def _same_frozen_input_basis(
    baseline: DecisionBundleV1,
    other: DecisionBundleV1,
) -> bool:
    """Check whether two bundles share the same frozen decision evidence.

    Compares the provenance fields that materially define the decision basis:
    official snapshot identity/hash, projection artefact hash/version/generation,
    availability assessment reference/cutoff, and code revision.

    ``config_fingerprint`` is excluded: it is an opaque caller-supplied value
    whose contents are not defined by this repository, so we cannot prove that
    scenario-specific configuration is excluded from it.

    Intended scenario constraint differences (e.g. forced_captain) are not
    compared — only the input evidence that the optimiser consumed.
    """
    bp = baseline.inputs
    op = other.inputs
    return (
        bp.official_snapshot_id == op.official_snapshot_id
        and bp.official_snapshot_sha256 == op.official_snapshot_sha256
        and bp.projection_sha256 == op.projection_sha256
        and bp.projection_model_version == op.projection_model_version
        and bp.projection_generated_at == op.projection_generated_at
        and bp.availability_assessment_reference == op.availability_assessment_reference
        and bp.availability_cutoff_at == op.availability_cutoff_at
        and baseline.code_revision == other.code_revision
    )


def _validate_run_bundle_provenance(
    run: DecisionRun,
    bundle: DecisionBundleV1,
    label: str,
) -> None:
    """Validate that a DecisionRun and its paired DecisionBundleV1 agree on
    shared provenance fields that both contracts independently carry.

    A mismatch indicates the pair was assembled incorrectly — the run's
    provenance does not match the evidence the bundle preserves.
    """
    mismatches: list[str] = []
    if run.code_revision != bundle.code_revision:
        mismatches.append(
            f"code_revision: run={run.code_revision!r} != bundle={bundle.code_revision!r}"
        )
    if run.config_fingerprint != bundle.config_fingerprint:
        mismatches.append(
            f"config_fingerprint: run={run.config_fingerprint!r} != "
            f"bundle={bundle.config_fingerprint!r}"
        )
    if run.season is not None and run.season != bundle.season:
        mismatches.append(
            f"season: run={run.season!r} != bundle={bundle.season!r}"
        )
    if run.gameweek != bundle.gameweek:
        mismatches.append(
            f"gameweek: run={run.gameweek.value} != bundle={bundle.gameweek.value}"
        )
    if mismatches:
        raise ValueError(
            f"{label} run/bundle provenance mismatch: {'; '.join(mismatches)}"
        )


class LeakageError(Exception):
    """Raised when the evaluator detects temporal or provenance leakage."""


class MissingOutcomeError(Exception):
    """Raised when required outcome evidence is absent for a preserved candidate."""


class MissingFrozenScoreError(Exception):
    """Raised when a human override cannot be traced to preserved pre-deadline evidence."""


def evaluate_decision(
    *,
    baseline_bundle: DecisionBundleV1,
    baseline_run: DecisionRun,
    scenarios: tuple[tuple[DecisionRun, DecisionBundleV1], ...] = (),
    candidate_bundles: tuple[DecisionBundleV1, ...] = (),
    human_choice: SubmittedDecision | None = None,
    human_deviation_reasons: tuple[str, ...] = (),
    outcome: OutcomeEvidenceV1,
) -> DecisionEvaluationV1:
    """Evaluate a frozen gameweek decision against post-deadline outcome evidence.

    ``candidate_bundles`` carries separately preserved pre-deadline candidate
    bundles whose frozen projected xPts may be used to resolve the human
    decision's projected score when it matches neither the baseline nor a
    preserved scenario.  These bundles are subject to the same temporal and
    provenance leakage rules; they are never represented as scenarios.

    This function enforces the governing invariant:
        Post-deadline information may score frozen decisions, but must never
        alter or reconstruct what was known or recommended before the deadline.

    It does not:
        - invoke the forecast provider
        - rerun the optimiser
        - regenerate scenarios
        - fetch current/latest data
        - infer missing historical projections from later evidence
    """

    leakage_checks: list[str] = []
    decision_cutoff = baseline_bundle.decision_at

    # --- Run/bundle consistency ---
    if baseline_run.id != baseline_bundle.decision_run_id:
        raise ValueError(
            "baseline_run.id does not match baseline_bundle.decision_run_id"
        )
    _validate_run_bundle_provenance(baseline_run, baseline_bundle, "baseline")

    for scenario_run, scenario_bundle in scenarios:
        if scenario_run.id != scenario_bundle.decision_run_id:
            raise ValueError(
                f"scenario_run.id {scenario_run.id} does not match "
                f"scenario_bundle.decision_run_id {scenario_bundle.decision_run_id}"
            )
        _validate_run_bundle_provenance(
            scenario_run, scenario_bundle, str(scenario_bundle.decision_run_id)
        )

    # --- Temporal validation: baseline ---
    if baseline_bundle.inputs.projection_generated_at > decision_cutoff:
        raise LeakageError(
            "baseline projection_generated_at is after decision_at"
        )
    if (
        baseline_bundle.inputs.availability_cutoff_at is not None
        and baseline_bundle.inputs.availability_cutoff_at > decision_cutoff
    ):
        raise LeakageError(
            "baseline availability_cutoff_at is after decision_at"
        )

    # --- Temporal validation: outcome is post-deadline ---
    if outcome.observed_at <= decision_cutoff:
        raise LeakageError("outcome observed_at must be after decision_cutoff")

    # --- Season/Gameweek consistency ---
    if outcome.season != baseline_bundle.season:
        raise LeakageError(
            f"outcome season {outcome.season} != baseline season {baseline_bundle.season}"
        )
    if outcome.gameweek != baseline_bundle.gameweek:
        raise LeakageError(
            f"outcome gameweek {outcome.gameweek.value} != "
            f"baseline gameweek {baseline_bundle.gameweek.value}"
        )

    # --- Scenario temporal validation ---
    for scenario_run, scenario_bundle in scenarios:
        # Scenario created after cutoff is rejected
        if scenario_run.created_at > decision_cutoff:
            raise LeakageError(
                f"scenario {scenario_bundle.decision_run_id} created after decision cutoff"
            )
        # Scenario decision_at must not be after cutoff
        if scenario_bundle.decision_at > decision_cutoff:
            raise LeakageError(
                f"scenario {scenario_bundle.decision_run_id} "
                "decision_at is after baseline decision cutoff"
            )
        # Scenario projection must not be after cutoff
        if scenario_bundle.inputs.projection_generated_at > decision_cutoff:
            raise LeakageError(
                f"scenario {scenario_bundle.decision_run_id} "
                "projection_generated_at is after decision cutoff"
            )
        # Scenario must share same frozen input basis (snapshot, projection,
        # availability, code revision)
        if not _same_frozen_input_basis(baseline_bundle, scenario_bundle):
            raise LeakageError(
                f"scenario {scenario_bundle.decision_run_id} "
                "does not share the same frozen input basis as baseline"
            )
        # Scenario must be same gameweek and season
        if scenario_bundle.season != baseline_bundle.season:
            raise LeakageError(
                f"scenario {scenario_bundle.decision_run_id} "
                f"season {scenario_bundle.season} != baseline"
            )
        if scenario_bundle.gameweek != baseline_bundle.gameweek:
            raise LeakageError(
                f"scenario {scenario_bundle.decision_run_id} "
                f"gameweek {scenario_bundle.gameweek.value} != baseline"
            )

    # --- Candidate bundle temporal validation ---
    for candidate_bundle in candidate_bundles:
        if candidate_bundle.inputs.projection_generated_at > decision_cutoff:
            raise LeakageError(
                f"candidate bundle {candidate_bundle.decision_run_id} "
                "projection_generated_at is after decision cutoff"
            )
        if (
            candidate_bundle.inputs.availability_cutoff_at is not None
            and candidate_bundle.inputs.availability_cutoff_at > decision_cutoff
        ):
            raise LeakageError(
                f"candidate bundle {candidate_bundle.decision_run_id} "
                "availability_cutoff_at is after decision cutoff"
            )
        if not _same_frozen_input_basis(baseline_bundle, candidate_bundle):
            raise LeakageError(
                f"candidate bundle {candidate_bundle.decision_run_id} "
                "does not share the same frozen input basis as baseline"
            )
        if candidate_bundle.season != baseline_bundle.season:
            raise LeakageError(
                f"candidate bundle {candidate_bundle.decision_run_id} "
                f"season {candidate_bundle.season} != baseline"
            )
        if candidate_bundle.gameweek != baseline_bundle.gameweek:
            raise LeakageError(
                f"candidate bundle {candidate_bundle.decision_run_id} "
                f"gameweek {candidate_bundle.gameweek.value} != baseline"
            )
        if candidate_bundle.decision_at > decision_cutoff:
            raise LeakageError(
                f"candidate bundle {candidate_bundle.decision_run_id} "
                "decision_at is after baseline decision cutoff"
            )

    # --- Obtain baseline outcome by selection identity ---
    baseline_outcome = outcome.outcome_for_identity(
        baseline_bundle.recommendation.identity
    )
    if baseline_outcome is None:
        raise MissingOutcomeError(
            "no outcome evidence matching baseline frozen selection identity"
        )

    baseline_realised = baseline_outcome.realised_points
    baseline_projected = baseline_bundle.recommendation.primary_objective

    baseline_solver_status = baseline_bundle.recommendation.solver_status
    # Only the explicit "Optimal" solver status proves optimality; other
    # statuses (Feasible, Heuristic, etc.) provide no proof either way.
    baseline_proven_optimal: bool | None = (
        True if baseline_solver_status == "Optimal" else None
    )

    baseline_eval = BaselineEvaluation(
        decision_run_id=baseline_bundle.decision_run_id,
        projected_points=baseline_projected,
        realised_points=baseline_realised,
        projected_vs_realised_residual=baseline_realised - baseline_projected,
        frozen_projection_generated_at=baseline_bundle.inputs.projection_generated_at,
        optimiser_status=baseline_solver_status,
        baseline_proven_optimal=baseline_proven_optimal,
    )

    leakage_checks.append("baseline_projection_before_cutoff")
    leakage_checks.append("outcome_after_decision_cutoff")

    # --- Scenario evaluations ---
    scenario_evals: list[ScenarioEvaluation] = []
    for scenario_run, scenario_bundle in scenarios:
        scenario_outcome = outcome.outcome_for_identity(
            scenario_bundle.recommendation.identity
        )
        if scenario_outcome is None:
            raise MissingOutcomeError(
                f"no outcome evidence matching scenario "
                f"{scenario_bundle.decision_run_id} frozen selection identity"
            )

        scenario_projected = scenario_bundle.recommendation.primary_objective
        scenario_realised = scenario_outcome.realised_points
        scenario_delta = scenario_projected - baseline_projected

        scenario_evals.append(
            ScenarioEvaluation(
                decision_run_id=scenario_bundle.decision_run_id,
                scenario_id=str(scenario_bundle.decision_run_id),
                projected_points=scenario_projected,
                projected_delta_vs_baseline=scenario_delta,
                realised_points=scenario_realised,
                projected_vs_realised_residual=scenario_realised - scenario_projected,
                frozen_projection_generated_at=scenario_bundle.inputs.projection_generated_at,
                optimiser_status=scenario_bundle.recommendation.solver_status,
                optimiser_settings_summary=scenario_run.optimiser_settings,
            )
        )

        leakage_checks.append(
            f"scenario_{scenario_bundle.decision_run_id}_same_input_basis"
        )

    # --- Human decision evaluation ---
    if human_choice is None:
        human_choice = baseline_bundle.actual_choice

    # When no explicit human choice exists, default to the baseline
    # recommendation. This represents the common case where the manager
    # followed the model recommendation exactly.
    if human_choice is None:
        human_choice = SubmittedDecision(
            squad_ids=baseline_bundle.recommendation.squad_ids,
            starting_xi_ids=baseline_bundle.recommendation.starting_xi_ids,
            captain_id=baseline_bundle.recommendation.captain_id,
            vice_captain_id=baseline_bundle.recommendation.vice_captain_id,
            bench_ids=baseline_bundle.recommendation.bench_ids,
            recorded_at=baseline_bundle.decision_at,
        )

    # Any evaluated human choice must be genuinely pre-cutoff
    if human_choice.recorded_at > decision_cutoff:
        raise LeakageError(
            "human_choice.recorded_at is after decision_cutoff; "
            "post-deadline human choice cannot be evaluated"
        )

    # When baseline_bundle.actual_choice is preserved and an explicit
    # human_choice argument is supplied, they must agree. The evaluator
    # must not silently allow the explicit argument to replace
    # contradictory preserved evidence.
    if (
        baseline_bundle.actual_choice is not None
        and (
            human_choice.identity != baseline_bundle.actual_choice.identity
            or human_choice.recorded_at
            != baseline_bundle.actual_choice.recorded_at
        )
    ):
        raise ValueError(
            "explicit human_choice does not match preserved "
            "baseline_bundle.actual_choice; refusing to silently "
            "replace preserved pre-deadline evidence"
        )

    # Determine relationship to baseline
    human_matches_baseline = (
        human_choice.identity == baseline_bundle.recommendation.identity
    )

    # Determine relationship to scenarios
    human_matches_scenarios: list[str] = []
    for _scenario_run, scenario_bundle in scenarios:
        if human_choice.identity == scenario_bundle.recommendation.identity:
            human_matches_scenarios.append(str(scenario_bundle.decision_run_id))

    # Human's projected points: only from preserved pre-deadline evidence.
    # Track which artefact supplied the frozen projected score so the
    # ForecastObservation can attribute it to the correct source.
    human_projected: float | None = None
    human_projected_source_run_id = baseline_bundle.decision_run_id
    if human_matches_baseline:
        human_projected = baseline_projected
    else:
        for _scenario_run, scenario_bundle in scenarios:
            if human_choice.identity == scenario_bundle.recommendation.identity:
                human_projected = scenario_bundle.recommendation.primary_objective
                human_projected_source_run_id = scenario_bundle.decision_run_id
                break

        # Check separately preserved candidate bundles
        if human_projected is None:
            for candidate_bundle in candidate_bundles:
                if human_choice.identity == candidate_bundle.recommendation.identity:
                    human_projected = (
                        candidate_bundle.recommendation.primary_objective
                    )
                    human_projected_source_run_id = (
                        candidate_bundle.decision_run_id
                    )
                    break

    # If unmatched, we cannot reconstruct - must fail explicitly
    if human_projected is None and not human_matches_baseline:
        raise MissingFrozenScoreError(
            "human override does not match baseline, any preserved scenario, "
            "or any preserved candidate bundle; frozen projected score "
            "unavailable. Refusing to reconstruct."
        )

    # Human outcome — matched by selection identity, not by run_id
    human_realised: float | None = None

    # Search outcome candidates by selection identity match
    for candidate in outcome.candidates:
        if candidate.identity == human_choice.identity:
            human_realised = candidate.realised_points
            break

    if human_realised is None:
        raise MissingOutcomeError(
            "no outcome evidence matching human choice selection identity"
        )

    human_eval = HumanDecisionEvaluation(
        selection_identity_matches_baseline=human_matches_baseline,
        selection_identity_matches_scenario_ids=tuple(human_matches_scenarios),
        projected_points=human_projected,
        projected_delta_vs_baseline=(
            (human_projected - baseline_projected) if human_projected is not None else None
        ),
        realised_points=human_realised,
        rationale_reasons=human_deviation_reasons,
        projected_vs_realised_residual=(
            (human_realised - human_projected) if human_projected is not None else None
        ),
    )

    # --- Comparison section ---
    projected_override_cost: float | None = None
    if human_projected is not None:
        projected_override_cost = baseline_projected - human_projected

    realised_override_delta = human_realised - baseline_realised

    comparison = ComparisonSection(
        projected_override_cost=projected_override_cost,
        realised_override_delta=realised_override_delta,
    )

    # --- Forecast observations (residuals only) ---
    observations: list[ForecastObservation] = []

    observations.append(
        ForecastObservation(
            decision_run_id=baseline_bundle.decision_run_id,
            candidate_label="baseline",
            projected_points=baseline_projected,
            realised_points=baseline_realised,
            residual=baseline_realised - baseline_projected,
        )
    )

    for scenario_eval in scenario_evals:
        observations.append(
            ForecastObservation(
                decision_run_id=scenario_eval.decision_run_id,
                candidate_label=f"scenario_{scenario_eval.scenario_id}",
                projected_points=scenario_eval.projected_points,
                realised_points=scenario_eval.realised_points,
                residual=scenario_eval.realised_points - scenario_eval.projected_points,
            )
        )

    if human_projected is not None:
        observations.append(
            ForecastObservation(
                decision_run_id=human_projected_source_run_id,
                candidate_label="human_choice",
                projected_points=human_projected,
                realised_points=human_realised,
                residual=human_realised - human_projected,
            )
        )

    # --- Validation section ---
    # A scenario scoring more realised points is NOT an optimiser failure
    # if the baseline was optimal for the frozen forecasts/objective/constraints.
    # Only the preserved solver status can support a proven-optimality claim;
    # DecisionRun.status is application execution status, not solver evidence.
    validation = ValidationSection(
        optimiser_status=baseline_solver_status,
        baseline_proven_optimal=baseline_proven_optimal,
        same_input_comparison=all(
            _same_frozen_input_basis(baseline_bundle, scenario_bundle)
            for _, scenario_bundle in scenarios
        ),
        leakage_checks=tuple(leakage_checks),
        optimiser_failure_from_realised_outcome=False,
    )

    return DecisionEvaluationV1(
        season=baseline_bundle.season,
        gameweek=baseline_bundle.gameweek,
        decision_cutoff=decision_cutoff,
        baseline=baseline_eval,
        scenarios=tuple(scenario_evals),
        human_decision=human_eval,
        comparison=comparison,
        validation=validation,
        forecast_observations=tuple(observations),
    )
