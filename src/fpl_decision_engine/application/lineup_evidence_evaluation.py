"""Evaluate whether contemporary lineup evidence adds information beyond provider p_start."""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import numpy as np
from statsmodels.discrete.discrete_model import Logit  # type: ignore[reportMissingTypeStubs]
from statsmodels.stats import sandwich_covariance  # type: ignore[reportMissingTypeStubs]
from statsmodels.tools.sm_exceptions import (  # type: ignore[reportMissingTypeStubs]
    ConvergenceWarning,
    PerfectSeparationError,
    PerfectSeparationWarning,
)

from fpl_decision_engine.domain import (
    CALIBRATION_BINS,
    LOG_LOSS_EPSILON,
    PROTOCOL_VERSION,
    REGRESSION_EPSILON,
    CalibrationBin,
    ChronologyStatus,
    EvaluationConclusion,
    EvaluationDiagnosticReason,
    EvaluationExclusionReason,
    EvaluationRecord,
    EvidenceClassSummary,
    IncrementalModelResult,
    JoinedLineupOutcome,
    LineupEvidenceClass,
    LineupEvidenceEvaluationResult,
    LineupEvidenceStatus,
    OutcomeState,
    PopulationSummary,
    PredictiveComparison,
    ProviderBaseline,
    SampleSufficiency,
)


class _FitKind(StrEnum):
    INCREMENTAL = "incremental"
    PROVIDER_ONLY = "provider_only"


@dataclass(frozen=True)
class _Fit:
    coefficients: tuple[float, ...]
    probabilities: tuple[float, ...]
    robust_standard_errors: tuple[float, ...] | None
    cluster_count: int


@dataclass(frozen=True)
class _FitFailure:
    reason: EvaluationDiagnosticReason


FitResult = _Fit | _FitFailure

SPECIFICATION = (
    "logit(pi) = beta_0 + beta_p*logit(p_start*) + "
    "beta_S*I(SUPPORTS_START) + beta_B*I(SUPPORTS_BENCH)"
)
COVARIANCE_METHOD = "cluster_robust_CRV1_normal_95"
CLUSTER_KEY = "canonical_player_id"
_CODE_VERSION = "#94-statsmodels-evaluator-v1"


def _finite_probability(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 1


def _logit(value: float) -> float:
    return math.log(value / (1 - value))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _brier(probabilities: Sequence[float], outcomes: Sequence[int]) -> float | None:
    if not probabilities:
        return None
    return sum(
        (probability - outcome) ** 2
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ) / len(probabilities)


def _log_loss(probabilities: Sequence[float], outcomes: Sequence[int]) -> float | None:
    if not probabilities:
        return None
    clipped = (
        min(max(probability, LOG_LOSS_EPSILON), 1 - LOG_LOSS_EPSILON)
        for probability in probabilities
    )
    return -sum(
        outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability)
        for probability, outcome in zip(clipped, outcomes, strict=True)
    ) / len(probabilities)


def _canonical_probability(value: float | None) -> float | str | None:
    if value is None or math.isfinite(value):
        return value
    return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"


def _dataset_hash(records: Sequence[EvaluationRecord]) -> str:
    canonical = [
        {
            "season": record.season,
            "gameweek": record.gameweek,
            "canonical_player_id": str(record.canonical_player_id),
            "chronology_status": record.chronology_status.value,
            "provider_p_start": _canonical_probability(record.provider_p_start),
            "outcome_state": record.outcome_state.value if record.outcome_state else None,
            "evidence_status": record.evidence_status.value if record.evidence_status else None,
            "evidence_class": record.evidence_class.value if record.evidence_class else None,
        }
        for record in sorted(records, key=lambda item: item.logical_identity)
    ]
    content = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def _exclusion(record: EvaluationRecord) -> EvaluationExclusionReason | None:
    if record.chronology_status is ChronologyStatus.EXCLUDED_CHRONOLOGY:
        return EvaluationExclusionReason.CHRONOLOGY_EXCLUDED
    if record.chronology_status is ChronologyStatus.EXCLUDED_CHRONOLOGY_UNPROVEN:
        return EvaluationExclusionReason.CHRONOLOGY_UNPROVEN
    if record.provider_p_start is None:
        return EvaluationExclusionReason.MISSING_P_START
    if not _finite_probability(record.provider_p_start):
        return EvaluationExclusionReason.INVALID_P_START
    if record.outcome_state in (None, OutcomeState.MISSING):
        return EvaluationExclusionReason.MISSING_REALISED_OUTCOME
    if record.evidence_status is LineupEvidenceStatus.MISSING:
        return EvaluationExclusionReason.MISSING_EVIDENCE
    if record.evidence_status is None and record.evidence_class is None:
        return EvaluationExclusionReason.UNCLASSIFIED_EVIDENCE
    if record.evidence_status is LineupEvidenceStatus.CONFLICTING:
        return EvaluationExclusionReason.CONFLICTING_EVIDENCE
    if record.evidence_status is not LineupEvidenceStatus.CLASSIFIED:
        return EvaluationExclusionReason.UNCLASSIFIED_EVIDENCE
    if record.evidence_class not in tuple(LineupEvidenceClass):
        return EvaluationExclusionReason.UNCLASSIFIED_EVIDENCE
    return None


def _calibration(records: Sequence[EvaluationRecord]) -> tuple[CalibrationBin, ...]:
    result: list[CalibrationBin] = []
    for lower, upper, includes_upper in CALIBRATION_BINS:
        selected = [
            record
            for record in records
            if record.provider_p_start is not None
            and (
                lower <= record.provider_p_start < upper
                or includes_upper
                and record.provider_p_start == upper
            )
        ]
        probabilities = [
            record.provider_p_start
            for record in selected
            if record.provider_p_start is not None
        ]
        outcomes = [_outcome(record) for record in selected]
        mean_probability = _mean(probabilities)
        start_rate = _mean(outcomes)
        result.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                n=len(selected),
                mean_p_start=mean_probability,
                realised_start_rate=start_rate,
                observed_minus_predicted=(
                    start_rate - mean_probability
                    if start_rate is not None and mean_probability is not None
                    else None
                ),
                sparse=len(selected) < 10,
            )
        )
    return tuple(result)


def _outcome(record: EvaluationRecord) -> int:
    if record.outcome_state is OutcomeState.STARTED:
        return 1
    if record.outcome_state is OutcomeState.NON_START:
        return 0
    raise ValueError("primary records require a realised start state")


def _class_summary(
    records: Sequence[EvaluationRecord],
) -> dict[LineupEvidenceClass, EvidenceClassSummary]:
    summaries: dict[LineupEvidenceClass, EvidenceClassSummary] = {}
    for evidence_class in LineupEvidenceClass:
        selected = [record for record in records if record.evidence_class is evidence_class]
        probabilities = [
            record.provider_p_start
            for record in selected
            if record.provider_p_start is not None
        ]
        outcomes = [_outcome(record) for record in selected]
        summaries[evidence_class] = EvidenceClassSummary(
            n=len(selected),
            mean_p_start=_mean(probabilities),
            realised_start_rate=_mean(outcomes),
            brier_score=_brier(probabilities, outcomes),
        )
    return summaries


def _fit(
    records: Sequence[EvaluationRecord],
    *,
    kind: _FitKind,
    cluster_robust: bool = True,
) -> FitResult:

    cluster_ids = sorted({record.canonical_player_id for record in records}, key=str)
    if cluster_robust and len(cluster_ids) < 2:
        return _FitFailure(EvaluationDiagnosticReason.INSUFFICIENT_PLAYER_CLUSTERS)
    cluster_codes = {player_id: index for index, player_id in enumerate(cluster_ids)}
    outcomes = [_outcome(record) for record in records]
    provider_terms = [
        _logit(
            min(
                max(record.provider_p_start or 0, REGRESSION_EPSILON),
                1 - REGRESSION_EPSILON,
            )
        )
        for record in records
    ]
    if kind is _FitKind.PROVIDER_ONLY:
        design = [[1.0, provider] for provider in provider_terms]
    else:
        design = [
            [
                1.0,
                provider,
                float(record.evidence_class is LineupEvidenceClass.SUPPORTS_START),
                float(record.evidence_class is LineupEvidenceClass.SUPPORTS_BENCH),
            ]
            for record, provider in zip(records, provider_terms, strict=True)
        ]
    groups = [cluster_codes[record.canonical_player_id] for record in records]
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            fitted: Any = Logit(outcomes, design).fit(  # type: ignore[reportUnknownMemberType]
                start_params=[0.0] * len(design[0]),
                method="newton",
                maxiter=200,
                disp=False,
                full_output=True,
            )
        warning_text = " ".join(str(item.message).lower() for item in captured)
        if any(isinstance(item.message, ConvergenceWarning) for item in captured):
            return _FitFailure(EvaluationDiagnosticReason.NON_CONVERGENCE)
        mle_retvals: dict[str, object] = dict(getattr(fitted, "mle_retvals", {}))
        if not bool(mle_retvals.get("converged", False)):
            if "separation" in warning_text or any(
                isinstance(item.message, PerfectSeparationWarning) for item in captured
            ):
                return _FitFailure(
                    EvaluationDiagnosticReason.COMPLETE_OR_QUASI_COMPLETE_SEPARATION
                )
            return _FitFailure(EvaluationDiagnosticReason.NON_CONVERGENCE)
        if "separation" in warning_text:
            return _FitFailure(EvaluationDiagnosticReason.COMPLETE_OR_QUASI_COMPLETE_SEPARATION)
        coefficients = tuple(float(value) for value in fitted.params)
        if not all(math.isfinite(value) for value in coefficients):
            return _FitFailure(EvaluationDiagnosticReason.INVALID_COEFFICIENTS)
        if cluster_robust:
            try:
                covariance: Any = sandwich_covariance.cov_cluster(  # type: ignore[reportUnknownMemberType]
                    fitted._results,  # type: ignore[reportPrivateUsage]
                    groups,
                    use_correction=False,
                )
                covariance_array = np.asarray(cast(Any, covariance), dtype=np.float64)
                expected_shape = (len(design[0]), len(design[0]))
                if covariance_array.shape != expected_shape:
                    return _FitFailure(EvaluationDiagnosticReason.COVARIANCE_FAILURE)
                with np.errstate(invalid="raise"):
                    standard_errors = tuple(
                        float(value)
                        for value in np.sqrt(
                            np.diag(covariance_array)  # type: ignore[reportUnknownArgumentType]
                        )
                    )
            except (ValueError, np.linalg.LinAlgError, TypeError, FloatingPointError):
                return _FitFailure(EvaluationDiagnosticReason.COVARIANCE_FAILURE)
            if len(standard_errors) != len(coefficients):
                return _FitFailure(EvaluationDiagnosticReason.INVALID_ROBUST_VARIANCE)
            if not all(math.isfinite(value) and value >= 0 for value in standard_errors):
                return _FitFailure(EvaluationDiagnosticReason.INVALID_ROBUST_VARIANCE)
        else:
            standard_errors = None
        probabilities = tuple(float(value) for value in fitted.predict(design))
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in probabilities):
            return _FitFailure(EvaluationDiagnosticReason.INVALID_COEFFICIENTS)
        return _Fit(
            coefficients=coefficients,
            probabilities=probabilities,
            robust_standard_errors=standard_errors,
            cluster_count=len(cluster_ids),
        )
    except PerfectSeparationError:
        return _FitFailure(EvaluationDiagnosticReason.COMPLETE_OR_QUASI_COMPLETE_SEPARATION)
    except (ValueError, np.linalg.LinAlgError):
        return _FitFailure(EvaluationDiagnosticReason.SINGULAR_DESIGN)
    except Exception:
        return _FitFailure(EvaluationDiagnosticReason.NUMERICAL_FAILURE)


def _odds_ratio_interval(
    coefficient: float, standard_error: float
) -> tuple[float, tuple[float, float]] | None:
    try:
        result = (
            math.exp(coefficient),
            (
                math.exp(coefficient - 1.96 * standard_error),
                math.exp(coefficient + 1.96 * standard_error),
            ),
        )
    except (OverflowError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (result[0], *result[1])):
        return None
    return result


def _incremental_result(
    records: Sequence[EvaluationRecord],
) -> tuple[IncrementalModelResult, _Fit | None]:
    fitted = _fit(records, kind=_FitKind.INCREMENTAL)
    cluster_count = len({record.canonical_player_id for record in records})
    if isinstance(fitted, _Fit):
        assert fitted.robust_standard_errors is not None
        start_result = _odds_ratio_interval(
            fitted.coefficients[2], fitted.robust_standard_errors[2]
        )
        bench_result = _odds_ratio_interval(
            fitted.coefficients[3], fitted.robust_standard_errors[3]
        )
        if start_result is None or bench_result is None:
            return (
                IncrementalModelResult(
                    specification=SPECIFICATION,
                    reference_category="NO_MATERIAL_SIGNAL",
                    estimator="ordinary_unpenalized_logit_mle",
                    solver="newton",
                    max_iterations=200,
                    converged=False,
                    diagnostic_reason=EvaluationDiagnosticReason.INVALID_CONFIDENCE_INTERVAL,
                    covariance_method=COVARIANCE_METHOD,
                    cluster_key=CLUSTER_KEY,
                    cluster_count=cluster_count,
                ),
                None,
            )
        start_or, start_ci = start_result
        bench_or, bench_ci = bench_result
        return (
            IncrementalModelResult(
                specification=SPECIFICATION,
                reference_category="NO_MATERIAL_SIGNAL",
                estimator="ordinary_unpenalized_logit_mle",
                solver="newton",
                max_iterations=200,
                converged=True,
                beta_intercept=fitted.coefficients[0],
                beta_provider=fitted.coefficients[1],
                beta_supports_start=fitted.coefficients[2],
                beta_supports_bench=fitted.coefficients[3],
                or_supports_start=start_or,
                or_supports_start_ci95=start_ci,
                or_supports_bench=bench_or,
                or_supports_bench_ci95=bench_ci,
                covariance_method=COVARIANCE_METHOD,
                cluster_key=CLUSTER_KEY,
                cluster_count=fitted.cluster_count,
            ),
            fitted,
        )
    return (
        IncrementalModelResult(
            specification=SPECIFICATION,
            reference_category="NO_MATERIAL_SIGNAL",
            estimator="ordinary_unpenalized_logit_mle",
            solver="newton",
            max_iterations=200,
            converged=False,
            diagnostic_reason=fitted.reason,
            covariance_method=COVARIANCE_METHOD,
            cluster_key=CLUSTER_KEY,
            cluster_count=cluster_count,
        ),
        None,
    )


class LineupEvidenceStatisticalEvaluator:
    """Run the fixed #94 protocol and return only Touchline-owned result contracts."""

    def __init__(self, *, code_version: str = _CODE_VERSION) -> None:
        self._code_version = code_version

    def evaluate(self, records: Sequence[EvaluationRecord]) -> LineupEvidenceEvaluationResult:
        """Evaluate supplied records with deterministic exclusions and fail-closed fitting."""

        input_dataset_identity = _dataset_hash(records)
        exclusions: Counter[EvaluationExclusionReason] = Counter()
        primary: list[EvaluationRecord] = []
        for record in records:
            reason = _exclusion(record)
            if reason is None:
                primary.append(record)
            else:
                exclusions[reason] += 1
        primary.sort(key=lambda item: item.logical_identity)
        identities = [record.logical_identity for record in primary]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate primary player/Gameweek observation")
        if len(records) != len(primary) + sum(exclusions.values()):
            raise ValueError("evaluation population does not reconcile supplied records")
        population = PopulationSummary(
            supplied=len(records),
            primary=len(primary),
            distinct_players=len({record.canonical_player_id for record in primary}),
            exclusions_by_reason=dict(sorted(exclusions.items(), key=lambda item: item[0].value)),
        )
        probabilities = [
            record.provider_p_start
            for record in primary
            if record.provider_p_start is not None
        ]
        outcomes = [_outcome(record) for record in primary]
        provider_baseline = ProviderBaseline(
            n=len(primary),
            brier_score=_brier(probabilities, outcomes),
            log_loss=_log_loss(probabilities, outcomes),
            mean_p_start=_mean(probabilities),
            realised_start_rate=_mean(outcomes),
        )
        incremental_model, incremental_fit = _incremental_result(primary)
        provider_fit = (
            _fit(primary, kind=_FitKind.PROVIDER_ONLY, cluster_robust=False)
            if primary
            else None
        )
        provider_only_brier = (
            _brier(provider_fit.probabilities, outcomes)
            if isinstance(provider_fit, _Fit)
            else None
        )
        evidence_brier = (
            _brier(incremental_fit.probabilities, outcomes)
            if incremental_fit is not None
            else None
        )
        comparison = PredictiveComparison(
            provider_only_fitted_brier=provider_only_brier,
            provider_plus_evidence_fitted_brier=evidence_brier,
            delta_brier=(
                provider_only_brier - evidence_brier
                if provider_only_brier is not None and evidence_brier is not None
                else None
            ),
        )
        class_counts = {
            evidence_class: sum(record.evidence_class is evidence_class for record in primary)
            for evidence_class in LineupEvidenceClass
        }
        estimation_pass = incremental_fit is not None and isinstance(provider_fit, _Fit)
        sufficiency = SampleSufficiency(
            total_n_pass=len(primary) >= 100,
            distinct_players_pass=population.distinct_players >= 30,
            supports_start_pass=class_counts[LineupEvidenceClass.SUPPORTS_START] >= 20,
            supports_bench_pass=class_counts[LineupEvidenceClass.SUPPORTS_BENCH] >= 20,
            no_material_signal_pass=class_counts[LineupEvidenceClass.NO_MATERIAL_SIGNAL] >= 20,
            estimation_pass=estimation_pass,
            overall=(
                len(primary) >= 100
                and population.distinct_players >= 30
                and all(count >= 20 for count in class_counts.values())
                and estimation_pass
            ),
        )
        comparison_material = input_dataset_identity + PROTOCOL_VERSION + self._code_version
        analysis_identity = f"sha256:{hashlib.sha256(comparison_material.encode()).hexdigest()}"
        return LineupEvidenceEvaluationResult(
            analysis_identity=analysis_identity,
            protocol_version=PROTOCOL_VERSION,
            input_dataset_identity=f"sha256:{input_dataset_identity}",
            supplied_record_count=len(records),
            primary_record_count=len(primary),
            code_version=self._code_version,
            calibration_bins=CALIBRATION_BINS,
            log_loss_epsilon=LOG_LOSS_EPSILON,
            regression_epsilon=REGRESSION_EPSILON,
            evidence_vocabulary_version="lineup-evidence-v1",
            population=population,
            provider_baseline=provider_baseline,
            calibration=_calibration(primary),
            evidence_classes=_class_summary(primary),
            incremental_model=incremental_model,
            predictive_comparison=comparison,
            sample_sufficiency=sufficiency,
            conclusion=self._conclusion(sufficiency, incremental_model, comparison),
        )

    @staticmethod
    def _conclusion(
        sufficiency: SampleSufficiency,
        model: IncrementalModelResult,
        comparison: PredictiveComparison,
    ) -> EvaluationConclusion:
        if not sufficiency.overall:
            return EvaluationConclusion.INSUFFICIENT_EVIDENCE
        assert model.or_supports_start is not None
        assert model.or_supports_start_ci95 is not None
        assert model.or_supports_bench is not None
        assert model.or_supports_bench_ci95 is not None
        assert comparison.delta_brier is not None
        positive = (
            model.or_supports_start > 1
            and model.or_supports_start_ci95[0] > 1
            and model.or_supports_bench < 1
            and model.or_supports_bench_ci95[1] < 1
            and comparison.delta_brier >= 0
        )
        both_intervals_include_null = (
            model.or_supports_start_ci95[0] <= 1 <= model.or_supports_start_ci95[1]
            and model.or_supports_bench_ci95[0] <= 1 <= model.or_supports_bench_ci95[1]
        )
        null = both_intervals_include_null and comparison.delta_brier <= 0
        if positive:
            return EvaluationConclusion.INCREMENTAL_VALUE_DETECTED
        if null:
            return EvaluationConclusion.NO_MATERIAL_INCREMENTAL_VALUE_DETECTED
        return EvaluationConclusion.MIXED_OR_INCONCLUSIVE


def records_from_joined(
    records: Sequence[JoinedLineupOutcome],
) -> tuple[EvaluationRecord, ...]:
    """Convert #93 joined records into the narrow #94 evaluator input."""

    return tuple(EvaluationRecord.from_joined(record) for record in records)
