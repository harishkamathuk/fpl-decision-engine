"""Touchline-owned contracts for #94 statistical evaluation."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import Field

from .base import DomainModel
from .lineup_evidence_validation import LineupEvidenceClass, LineupEvidenceStatus
from .lineup_outcomes import (
    ChronologyStatus,
    JoinedLineupOutcome,
    OutcomeState,
    RealisedOutcome,
)

PROTOCOL_VERSION = "09.01"
LOG_LOSS_EPSILON = 1e-15
REGRESSION_EPSILON = 1e-6
CALIBRATION_BINS: tuple[tuple[float, float, bool], ...] = (
    (0.0, 0.2, False),
    (0.2, 0.4, False),
    (0.4, 0.6, False),
    (0.6, 0.8, False),
    (0.8, 1.0, True),
)


class EvaluationExclusionReason(StrEnum):
    """Why a supplied row cannot enter the primary population."""

    CHRONOLOGY_EXCLUDED = "CHRONOLOGY_EXCLUDED"
    CHRONOLOGY_UNPROVEN = "CHRONOLOGY_UNPROVEN"
    MISSING_P_START = "MISSING_P_START"
    INVALID_P_START = "INVALID_P_START"
    MISSING_REALISED_OUTCOME = "MISSING_REALISED_OUTCOME"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    UNCLASSIFIED_EVIDENCE = "UNCLASSIFIED_EVIDENCE"


class EvaluationDiagnosticReason(StrEnum):
    """Bounded reasons an approved estimator cannot produce valid inference."""

    NON_CONVERGENCE = "NON_CONVERGENCE"
    COMPLETE_OR_QUASI_COMPLETE_SEPARATION = "COMPLETE_OR_QUASI_COMPLETE_SEPARATION"
    SINGULAR_DESIGN = "SINGULAR_DESIGN"
    INVALID_COEFFICIENTS = "INVALID_COEFFICIENTS"
    COVARIANCE_FAILURE = "COVARIANCE_FAILURE"
    INVALID_ROBUST_VARIANCE = "INVALID_ROBUST_VARIANCE"
    INVALID_CONFIDENCE_INTERVAL = "INVALID_CONFIDENCE_INTERVAL"
    INSUFFICIENT_PLAYER_CLUSTERS = "INSUFFICIENT_PLAYER_CLUSTERS"
    NUMERICAL_FAILURE = "NUMERICAL_FAILURE"


class EvaluationConclusion(StrEnum):
    """The four approved #94 conclusions."""

    INCREMENTAL_VALUE_DETECTED = "INCREMENTAL_VALUE_DETECTED"
    NO_MATERIAL_INCREMENTAL_VALUE_DETECTED = "NO_MATERIAL_INCREMENTAL_VALUE_DETECTED"
    MIXED_OR_INCONCLUSIVE = "MIXED_OR_INCONCLUSIVE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EvaluationRecord(DomainModel):
    """Narrow evaluator input, including states not constructible as valid #92 rows."""

    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: int = Field(ge=1)
    canonical_player_id: UUID
    chronology_status: ChronologyStatus
    provider_p_start: float | None = None
    outcome_state: OutcomeState | None = None
    evidence_status: LineupEvidenceStatus | None = None
    evidence_class: LineupEvidenceClass | None = None
    actual_minutes: int | None = Field(default=None, ge=0)

    @classmethod
    def from_joined(cls, joined: JoinedLineupOutcome) -> EvaluationRecord:
        """Project a #93 joined record without changing any source semantics."""

        return cls(
            season=joined.observation.season,
            gameweek=joined.observation.gameweek.value,
            canonical_player_id=joined.observation.canonical_player_id,
            chronology_status=joined.chronology.status,
            provider_p_start=joined.observation.original_p_start,
            outcome_state=joined.outcome_state,
            evidence_status=joined.observation.evidence_status,
            evidence_class=joined.observation.evidence_class,
            actual_minutes=(
                joined.outcome.minutes
                if isinstance(joined.outcome, RealisedOutcome)
                else None
            ),
        )

    @property
    def logical_identity(self) -> tuple[str, int, UUID]:
        return self.season, self.gameweek, self.canonical_player_id


class PopulationSummary(DomainModel):
    supplied: int
    primary: int
    distinct_players: int
    exclusions_by_reason: dict[EvaluationExclusionReason, int]


class ProviderBaseline(DomainModel):
    n: int
    brier_score: float | None
    log_loss: float | None
    mean_p_start: float | None
    realised_start_rate: float | None


class CalibrationBin(DomainModel):
    lower: float
    upper: float
    n: int
    mean_p_start: float | None
    realised_start_rate: float | None
    observed_minus_predicted: float | None
    sparse: bool


class EvidenceClassSummary(DomainModel):
    n: int
    mean_p_start: float | None
    realised_start_rate: float | None
    brier_score: float | None


class IncrementalModelResult(DomainModel):
    specification: str
    reference_category: str = "NO_MATERIAL_SIGNAL"
    estimator: str = "ordinary_unpenalized_logit_mle"
    solver: str = "newton"
    max_iterations: int = 200
    converged: bool
    diagnostic_reason: EvaluationDiagnosticReason | None = None
    beta_intercept: float | None = None
    beta_provider: float | None = None
    beta_supports_start: float | None = None
    beta_supports_bench: float | None = None
    or_supports_start: float | None = None
    or_supports_start_ci95: tuple[float, float] | None = None
    or_supports_bench: float | None = None
    or_supports_bench_ci95: tuple[float, float] | None = None
    covariance_method: str
    cluster_key: str
    cluster_count: int


class PredictiveComparison(DomainModel):
    provider_only_fitted_brier: float | None
    provider_plus_evidence_fitted_brier: float | None
    delta_brier: float | None
    interpretation: str = "IN_SAMPLE_DESCRIPTIVE"


class SampleSufficiency(DomainModel):
    total_n_pass: bool
    distinct_players_pass: bool
    supports_start_pass: bool
    supports_bench_pass: bool
    no_material_signal_pass: bool
    estimation_pass: bool
    overall: bool


class LineupEvidenceEvaluationResult(DomainModel):
    """Complete machine-readable semantic result for #94, not #95 persistence."""

    analysis_identity: str
    protocol_version: str
    input_dataset_identity: str
    supplied_record_count: int
    primary_record_count: int
    code_version: str
    evidence_vocabulary_version: str
    calibration_bins: tuple[tuple[float, float, bool], ...]
    log_loss_epsilon: float
    regression_epsilon: float
    population: PopulationSummary
    provider_baseline: ProviderBaseline
    calibration: tuple[CalibrationBin, ...]
    evidence_classes: dict[LineupEvidenceClass, EvidenceClassSummary]
    incremental_model: IncrementalModelResult
    predictive_comparison: PredictiveComparison
    sample_sufficiency: SampleSufficiency
    conclusion: EvaluationConclusion
