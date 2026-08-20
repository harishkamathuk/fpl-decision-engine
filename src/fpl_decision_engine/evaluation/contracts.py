"""Deterministic evaluation contracts for frozen decision assessment."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from fpl_decision_engine.domain.base import DomainModel
from fpl_decision_engine.domain.value_objects import GameweekNumber


class BaselineEvaluation(DomainModel):
    """Evaluation of the frozen baseline recommendation."""

    decision_run_id: UUID
    projected_points: float
    realised_points: float
    projected_vs_realised_residual: float
    frozen_projection_generated_at: AwareDatetime
    optimiser_status: str = Field(min_length=1)
    baseline_proven_optimal: bool | None = None


class ScenarioEvaluation(DomainModel):
    """Evaluation of one preserved pre-deadline scenario."""

    decision_run_id: UUID
    scenario_id: str = Field(min_length=1)
    projected_points: float
    projected_delta_vs_baseline: float
    realised_points: float
    projected_vs_realised_residual: float
    frozen_projection_generated_at: AwareDatetime
    optimiser_status: str = Field(min_length=1)
    optimiser_settings_summary: tuple[tuple[str, str], ...] = ()


class HumanDecisionEvaluation(DomainModel):
    """Evaluation of the final human choice."""

    selection_identity_matches_baseline: bool
    selection_identity_matches_scenario_ids: tuple[str, ...] = ()
    projected_points: float | None = None
    projected_delta_vs_baseline: float | None = None
    realised_points: float
    rationale_reasons: tuple[str, ...] = ()
    projected_vs_realised_residual: float | None = None


class ComparisonSection(DomainModel):
    """Projected and realised deltas between baseline and human choice."""

    projected_override_cost: float | None = None
    realised_override_delta: float | None = None


class ValidationSection(DomainModel):
    """Leakage and integrity checks."""

    optimiser_status: str = Field(min_length=1)
    baseline_proven_optimal: bool | None = None
    same_input_comparison: bool
    leakage_checks: tuple[str, ...] = ()
    optimiser_failure_from_realised_outcome: bool = False


class ForecastObservation(DomainModel):
    """Residual observation for one candidate — strictly not a systematic claim."""

    decision_run_id: UUID
    candidate_label: str = Field(min_length=1)
    projected_points: float
    realised_points: float
    residual: float


class DecisionEvaluationV1(DomainModel):
    """Immutable v1 deterministic evaluation of a frozen gameweek decision.

    Decision-time and outcome-time evidence are structurally separated.
    Post-deadline information scores frozen decisions but never alters or
    reconstructs what was known or recommended before the deadline.
    """

    schema_version: int = 1
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    decision_cutoff: AwareDatetime

    baseline: BaselineEvaluation
    scenarios: tuple[ScenarioEvaluation, ...] = ()
    human_decision: HumanDecisionEvaluation
    comparison: ComparisonSection
    validation: ValidationSection
    forecast_observations: tuple[ForecastObservation, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def supports_only_v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported evaluation schema_version {value}; supported: 1")
        return value

    @model_validator(mode="after")
    def validate_structural_integrity(self) -> Self:
        # Ensure leakage_checks is populated
        if not self.validation.leakage_checks:
            raise ValueError("validation.leakage_checks must not be empty")

        # Validate residuals are consistent
        baseline_residual = (
            self.baseline.realised_points - self.baseline.projected_points
        )
        if abs(self.baseline.projected_vs_realised_residual - baseline_residual) > 1e-9:
            raise ValueError(
                "baseline projected_vs_realised_residual inconsistent with points"
            )

        for scenario in self.scenarios:
            scenario_residual = scenario.realised_points - scenario.projected_points
            if abs(scenario.projected_vs_realised_residual - scenario_residual) > 1e-9:
                raise ValueError(
                    f"scenario {scenario.scenario_id} projected_vs_realised_residual "
                    "inconsistent with points"
                )

        # Validate comparison section
        if (
            self.comparison.projected_override_cost is not None
            and self.human_decision.projected_points is not None
        ):
            expected_cost = (
                self.baseline.projected_points - self.human_decision.projected_points
            )
            if abs(self.comparison.projected_override_cost - expected_cost) > 1e-9:
                raise ValueError("projected_override_cost inconsistent with baseline and human")

        if self.comparison.realised_override_delta is not None:
            expected_delta = (
                self.human_decision.realised_points - self.baseline.realised_points
            )
            if abs(self.comparison.realised_override_delta - expected_delta) > 1e-9:
                raise ValueError("realised_override_delta inconsistent with baseline and human")

        return self
