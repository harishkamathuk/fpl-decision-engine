"""Deterministic acceptance tests for the #94 statistical evaluator."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

import pytest
from statsmodels.discrete.discrete_model import Logit

from fpl_decision_engine.application import LineupEvidenceStatisticalEvaluator
from fpl_decision_engine.application import lineup_evidence_evaluation as evaluation_module
from fpl_decision_engine.domain import (
    ChronologyDecision,
    ChronologyStatus,
    EvaluationConclusion,
    EvaluationDiagnosticReason,
    EvaluationExclusionReason,
    EvaluationRecord,
    ExternalRef,
    GameweekNumber,
    JoinedLineupOutcome,
    LineupEvidenceClass,
    LineupEvidenceStatus,
    OutcomeState,
    RealisedOutcome,
)


def record(
    index: int,
    *,
    p_start: float | None = 0.5,
    outcome: OutcomeState | None = OutcomeState.STARTED,
    evidence_class: LineupEvidenceClass | None = LineupEvidenceClass.NO_MATERIAL_SIGNAL,
    evidence_status: LineupEvidenceStatus | None = LineupEvidenceStatus.CLASSIFIED,
    chronology: ChronologyStatus = ChronologyStatus.VALID,
    minutes: int | None = 90,
    gameweek: int | None = None,
) -> EvaluationRecord:
    return EvaluationRecord(
        season="2026-27",
        gameweek=gameweek or index + 1,
        canonical_player_id=UUID(int=index + 1),
        chronology_status=chronology,
        provider_p_start=p_start,
        outcome_state=outcome,
        evidence_status=evidence_status,
        evidence_class=evidence_class,
        actual_minutes=minutes,
    )


def valid_records(
    count: int = 120,
    *,
    minutes_offset: int = 0,
    outcome_mode: str = "balanced",
) -> list[EvaluationRecord]:
    records: list[EvaluationRecord] = []
    classes = tuple(LineupEvidenceClass)
    for index in range(count):
        evidence_class = classes[index % len(classes)]
        if outcome_mode == "positive":
            started = (
                index % 5 != 0
                if evidence_class is LineupEvidenceClass.SUPPORTS_START
                else index % 5 == 0
                if evidence_class is LineupEvidenceClass.SUPPORTS_BENCH
                else index % 2 == 0
            )
        else:
            started = (index * 7 + index // 3) % 5 < 2
        records.append(
            record(
                index,
                p_start=(index % 9 + 1) / 10,
                outcome=OutcomeState.STARTED if started else OutcomeState.NON_START,
                evidence_class=evidence_class,
                minutes=(90 if started else 0) + minutes_offset,
                gameweek=index // 40 + 1,
            )
        )
    return records


def deterministic_effect_records(
    start_effect: float,
    bench_effect: float,
) -> list[EvaluationRecord]:
    """Generate fixed pseudo-random outcomes without a random dependency or seed."""

    rows: list[EvaluationRecord] = []
    for index in range(120):
        evidence_class = tuple(LineupEvidenceClass)[index % 3]
        probability = (index % 19 + 1) / 20
        linear_predictor = (
            -0.2
            + math.log(probability / (1 - probability))
            + (
                start_effect
                if evidence_class is LineupEvidenceClass.SUPPORTS_START
                else bench_effect
                if evidence_class is LineupEvidenceClass.SUPPORTS_BENCH
                else 0
            )
        )
        pseudo_u = ((index * 1103515245 + 12345) % 2_147_483_648) / 2_147_483_648
        started = pseudo_u < 1 / (1 + math.exp(-linear_predictor))
        rows.append(
            record(
                index,
                p_start=probability,
                outcome=OutcomeState.STARTED if started else OutcomeState.NON_START,
                evidence_class=evidence_class,
                minutes=90 if started else 0,
                gameweek=index // 40 + 1,
            )
        )
    return rows


def test_population_excludes_invalid_rows_and_reconciles_exactly() -> None:
    supplied = valid_records(6)
    supplied.extend(
        [
            record(1000, chronology=ChronologyStatus.EXCLUDED_CHRONOLOGY),
            record(1001, chronology=ChronologyStatus.EXCLUDED_CHRONOLOGY_UNPROVEN),
            record(1002, p_start=None),
            record(1003, p_start=float("nan")),
            record(1008, p_start=float("inf")),
            record(1004, outcome=OutcomeState.MISSING),
            record(1005, evidence_status=LineupEvidenceStatus.MISSING, evidence_class=None),
            record(1006, evidence_status=LineupEvidenceStatus.CONFLICTING, evidence_class=None),
            record(1007, evidence_status=None, evidence_class=None),
        ]
    )
    result = LineupEvidenceStatisticalEvaluator().evaluate(supplied)

    assert result.population.supplied == len(supplied)
    assert result.population.primary == 6
    assert sum(result.population.exclusions_by_reason.values()) == 9
    assert (
        result.population.exclusions_by_reason[EvaluationExclusionReason.CHRONOLOGY_EXCLUDED]
        == 1
    )
    assert (
        result.population.exclusions_by_reason[EvaluationExclusionReason.CHRONOLOGY_UNPROVEN]
        == 1
    )
    assert result.population.exclusions_by_reason[EvaluationExclusionReason.MISSING_P_START] == 1
    assert result.population.exclusions_by_reason[EvaluationExclusionReason.INVALID_P_START] == 2
    assert (
        result.population.exclusions_by_reason[EvaluationExclusionReason.MISSING_REALISED_OUTCOME]
        == 1
    )
    assert result.population.exclusions_by_reason[EvaluationExclusionReason.MISSING_EVIDENCE] == 1
    assert (
        result.population.exclusions_by_reason[EvaluationExclusionReason.CONFLICTING_EVIDENCE]
        == 1
    )
    assert (
        result.population.exclusions_by_reason[EvaluationExclusionReason.UNCLASSIFIED_EVIDENCE]
        == 1
    )
    assert result.population.supplied == result.population.primary + sum(
        result.population.exclusions_by_reason.values()
    )
    assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE


def test_duplicate_primary_player_gameweek_fails_explicitly() -> None:
    first = record(1, gameweek=1)
    duplicate = first.model_copy()
    with pytest.raises(ValueError, match="duplicate primary"):
        LineupEvidenceStatisticalEvaluator().evaluate([first, duplicate])


def test_start_and_nonstart_encode_as_binary_outcomes() -> None:
    result = LineupEvidenceStatisticalEvaluator().evaluate(
        [
            record(0, p_start=0.25, outcome=OutcomeState.STARTED),
            record(1, p_start=0.75, outcome=OutcomeState.NON_START),
        ]
    )

    assert result.provider_baseline.realised_start_rate == 0.5
    assert result.evidence_classes[
        LineupEvidenceClass.NO_MATERIAL_SIGNAL
    ].brier_score == pytest.approx(((0.25 - 1) ** 2 + (0.75 - 0) ** 2) / 2)


def test_baseline_brier_log_loss_and_boundary_calibration_are_exact() -> None:
    probabilities = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    outcomes = (
        OutcomeState.NON_START,
        OutcomeState.STARTED,
        OutcomeState.NON_START,
        OutcomeState.STARTED,
        OutcomeState.STARTED,
        OutcomeState.NON_START,
    )
    records = [
        record(index, p_start=probability, outcome=outcomes[index])
        for index, probability in enumerate(probabilities)
    ]
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    expected_brier = sum(
        (probability - int(outcome is OutcomeState.STARTED)) ** 2
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ) / len(probabilities)
    expected_log_loss = -sum(
        int(outcome is OutcomeState.STARTED) * math.log(max(probability, 1e-15))
        + int(outcome is OutcomeState.NON_START)
        * math.log(1 - min(probability, 1 - 1e-15))
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ) / len(probabilities)
    assert result.provider_baseline.brier_score == pytest.approx(expected_brier)
    assert result.provider_baseline.log_loss == pytest.approx(expected_log_loss)
    assert [item.n for item in result.calibration] == [1, 1, 1, 1, 2]
    assert [item.lower for item in result.calibration] == [0.0, 0.2, 0.4, 0.6, 0.8]
    assert [item.sparse for item in result.calibration] == [True] * 5


def test_all_fixed_bins_report_sparse_and_class_summaries_preserve_vocabulary() -> None:
    records = [
        record(
            index,
            p_start=(index % 5) * 0.2,
            outcome=OutcomeState.STARTED if index % 2 else OutcomeState.NON_START,
            evidence_class=tuple(LineupEvidenceClass)[index % 3],
        )
        for index in range(15)
    ]
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    assert len(result.calibration) == 5
    assert all(item.sparse for item in result.calibration)
    assert set(result.evidence_classes) == set(LineupEvidenceClass)
    assert sum(item.n for item in result.evidence_classes.values()) == 15
    assert result.evidence_classes[LineupEvidenceClass.SUPPORTS_START].n == 5


def test_minutes_do_not_affect_primary_metrics_or_semantic_identity() -> None:
    first = LineupEvidenceStatisticalEvaluator().evaluate(valid_records(minutes_offset=0))
    second = LineupEvidenceStatisticalEvaluator().evaluate(valid_records(minutes_offset=30))

    assert first.provider_baseline == second.provider_baseline
    assert first.calibration == second.calibration
    assert first.evidence_classes == second.evidence_classes
    assert first.incremental_model == second.incremental_model
    assert first.predictive_comparison == second.predictive_comparison
    assert first.conclusion is second.conclusion
    assert first.input_dataset_identity == second.input_dataset_identity


def test_evaluator_replays_identically_and_emits_no_forecast_outputs() -> None:
    records = valid_records()
    first = LineupEvidenceStatisticalEvaluator().evaluate(records)
    second = LineupEvidenceStatisticalEvaluator().evaluate(list(reversed(records)))

    assert first == second
    dumped = first.model_dump()
    assert "p_start" not in dumped
    assert "expected_minutes" not in dumped
    assert "expected_points" not in dumped
    assert "optimiser" not in dumped
    assert first.input_dataset_identity.startswith("sha256:")
    assert first.analysis_identity.startswith("sha256:")
    assert first.supplied_record_count == 120
    assert first.primary_record_count == 120
    assert first.log_loss_epsilon == 1e-15
    assert first.regression_epsilon == 1e-6
    assert first.calibration_bins[-1] == (0.8, 1.0, True)


def test_provider_fitted_brier_and_regression_clipping_match_reference_fit() -> None:
    records = valid_records()
    records[0] = records[0].model_copy(update={"provider_p_start": 0.0})
    records[1] = records[1].model_copy(update={"provider_p_start": 1.0})
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    clipped = [
        min(max(item.provider_p_start or 0, 1e-6), 1 - 1e-6)
        for item in records
    ]
    design = [[1.0, math.log(probability / (1 - probability))] for probability in clipped]
    outcomes = [int(item.outcome_state is OutcomeState.STARTED) for item in records]
    reference = Logit(outcomes, design).fit(
        start_params=[0.0, 0.0],
        method="newton",
        maxiter=200,
        disp=False,
        full_output=True,
    )
    reference_brier = sum(
        (float(probability) - outcome) ** 2
        for probability, outcome in zip(reference.predict(design), outcomes, strict=True)
    ) / len(records)

    assert result.predictive_comparison.provider_only_fitted_brier == pytest.approx(
        reference_brier
    )
    assert records[0].provider_p_start == 0.0
    assert records[1].provider_p_start == 1.0
    assert result.incremental_model.beta_provider is not None
    assert math.isfinite(result.incremental_model.beta_provider)


def test_regression_uses_prespecified_model_and_player_clusters() -> None:
    result = LineupEvidenceStatisticalEvaluator().evaluate(valid_records())

    assert result.incremental_model.specification.startswith("logit(pi)")
    assert result.incremental_model.estimator == "ordinary_unpenalized_logit_mle"
    assert result.incremental_model.solver == "newton"
    assert result.incremental_model.max_iterations == 200
    assert result.incremental_model.reference_category == "NO_MATERIAL_SIGNAL"
    assert "I(SUPPORTS_START)" in result.incremental_model.specification
    assert "I(SUPPORTS_BENCH)" in result.incremental_model.specification
    assert "interaction" not in result.incremental_model.specification.lower()
    assert result.incremental_model.covariance_method == "cluster_robust_CRV1_normal_95"
    assert result.incremental_model.cluster_key == "canonical_player_id"
    assert result.incremental_model.cluster_count == 120
    assert result.incremental_model.converged
    assert result.incremental_model.or_supports_start is not None
    assert result.incremental_model.or_supports_bench is not None


def test_repeated_player_observations_are_clustered_not_deduplicated() -> None:
    records = valid_records(120)
    for index, item in enumerate(records):
        records[index] = item.model_copy(
            update={
                "canonical_player_id": UUID(int=(index % 40) + 1),
                "gameweek": index // 40 + 1,
            }
        )
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    assert result.population.primary == 120
    assert result.population.distinct_players == 40
    assert result.incremental_model.cluster_count == 40


def test_distinct_player_gate_fails_independently() -> None:
    records = valid_records()
    for index, item in enumerate(records):
        records[index] = item.model_copy(
            update={
                "canonical_player_id": UUID(int=(index % 20) + 1),
                "gameweek": index // 20 + 1,
            }
        )
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    assert result.sample_sufficiency.total_n_pass
    assert not result.sample_sufficiency.distinct_players_pass
    assert not result.sample_sufficiency.overall
    assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE


def test_sample_gates_fail_closed_without_reinterpreting_as_no_value() -> None:
    result = LineupEvidenceStatisticalEvaluator().evaluate(valid_records(30))

    assert not result.sample_sufficiency.total_n_pass
    assert not result.sample_sufficiency.overall
    assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(
    "evidence_class",
    tuple(LineupEvidenceClass),
)
def test_each_evidence_class_gate_is_independent(
    evidence_class: LineupEvidenceClass,
) -> None:
    records = valid_records()
    replacement = (
        LineupEvidenceClass.SUPPORTS_START
        if evidence_class is LineupEvidenceClass.NO_MATERIAL_SIGNAL
        else LineupEvidenceClass.NO_MATERIAL_SIGNAL
    )
    for index, item in enumerate(records):
        if item.evidence_class is evidence_class:
            records[index] = item.model_copy(update={"evidence_class": replacement})
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    assert not getattr(
        result.sample_sufficiency,
        {
            LineupEvidenceClass.SUPPORTS_START: "supports_start_pass",
            LineupEvidenceClass.SUPPORTS_BENCH: "supports_bench_pass",
            LineupEvidenceClass.NO_MATERIAL_SIGNAL: "no_material_signal_pass",
        }[evidence_class],
    )
    assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE


def test_covariance_failure_is_translated_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evaluation_module.sandwich_covariance,
        "cov_cluster",
        lambda *args, **kwargs: [[float("nan")] * 4 for _ in range(4)],
    )
    result = LineupEvidenceStatisticalEvaluator().evaluate(valid_records())

    assert not result.incremental_model.converged
    assert result.incremental_model.diagnostic_reason in {
        EvaluationDiagnosticReason.INVALID_ROBUST_VARIANCE,
        EvaluationDiagnosticReason.COVARIANCE_FAILURE,
    }
    assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE


def test_records_from_joined_preserves_outcome_state_and_minutes() -> None:
    from fpl_decision_engine.application import records_from_joined
    from fpl_decision_engine.domain import (
        LineupEvidenceProvenance,
        LineupEvidenceValidationObservation,
        Projection,
    )

    cutoff = datetime(2026, 8, 30, 12, tzinfo=UTC)
    projection = Projection(
        player_id=UUID(int=9001),
        gameweek=GameweekNumber(value=1),
        expected_points=5,
        expected_minutes=80,
        appearance_probability=0.9,
        start_probability=0.5,
        source="fpl-forecast",
        model_version="v1",
        generated_at=cutoff.replace(minute=58),
    )
    evidence = LineupEvidenceProvenance(
        provider_id="lineup",
        provider_version="v1",
        source_reference="fixture://evidence",
        raw_sha256="a" * 64,
        observed_at=cutoff.replace(minute=59),
        retrieved_at=cutoff.replace(minute=59),
    )
    observation = LineupEvidenceValidationObservation.from_projection(
        season="2026-27",
        projection=projection,
        projection_provider_version="v1",
        projection_source_reference="fixture://projection",
        projection_source_sha256="b" * 64,
        projection_snapshot_id="projection-1",
        projection_mapping_fingerprint="c" * 64,
        evidence_status=LineupEvidenceStatus.CLASSIFIED,
        evidence_class=LineupEvidenceClass.SUPPORTS_START,
        evidence=evidence,
    )
    outcome = RealisedOutcome(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        player_ref=ExternalRef(provider="fpl-element", external_id="9001"),
        canonical_player_id=UUID(int=9001),
        started=True,
        minutes=73,
        source_reference="fixture://event/1/live",
        provider_id="fpl",
        provider_version="v1",
        snapshot_id="outcome-1",
        retrieved_at=cutoff.replace(day=31),
        finalised_at=cutoff.replace(day=31),
    )
    joined = JoinedLineupOutcome(
        observation=observation,
        chronology=ChronologyDecision(
            status=ChronologyStatus.VALID,
            cutoff=cutoff,
        ),
        outcome=outcome,
        outcome_state=OutcomeState.STARTED,
    )

    converted = records_from_joined([joined])
    assert converted[0].outcome_state is OutcomeState.STARTED
    assert converted[0].actual_minutes == 73
    assert converted[0].provider_p_start == 0.5


def test_estimation_failure_is_explicit_for_separation() -> None:
    records = [
        record(
            index,
            p_start=0.5,
            outcome=OutcomeState.STARTED,
            evidence_class=LineupEvidenceClass.SUPPORTS_START,
            gameweek=index + 1,
        )
        for index in range(120)
    ]
    result = LineupEvidenceStatisticalEvaluator().evaluate(records)

    assert not result.incremental_model.converged
    assert result.incremental_model.diagnostic_reason in {
        EvaluationDiagnosticReason.COMPLETE_OR_QUASI_COMPLETE_SEPARATION,
        EvaluationDiagnosticReason.SINGULAR_DESIGN,
        EvaluationDiagnosticReason.NON_CONVERGENCE,
        EvaluationDiagnosticReason.INSUFFICIENT_PLAYER_CLUSTERS,
    }
    assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE


def test_conclusions_cover_positive_null_and_mixed_contracts() -> None:
    evaluator = LineupEvidenceStatisticalEvaluator()
    positive = evaluator.evaluate(deterministic_effect_records(2, -2))
    null = evaluator.evaluate(deterministic_effect_records(0, 0))
    mixed = evaluator.evaluate(deterministic_effect_records(-1, 1))

    assert positive.conclusion is EvaluationConclusion.INCREMENTAL_VALUE_DETECTED
    assert null.conclusion is EvaluationConclusion.NO_MATERIAL_INCREMENTAL_VALUE_DETECTED
    assert mixed.conclusion is EvaluationConclusion.MIXED_OR_INCONCLUSIVE


def test_conclusions_are_insufficient_when_total_sample_gate_fails() -> None:
    for mode in ("balanced", "positive"):
        result = LineupEvidenceStatisticalEvaluator().evaluate(
            valid_records(99, outcome_mode=mode)
        )
        assert result.conclusion is EvaluationConclusion.INSUFFICIENT_EVIDENCE
