"""Versioned contracts for preserving a GW decision and its submitted outcome."""

from __future__ import annotations

from math import isfinite
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationInfo, field_validator, model_validator

from .base import DomainModel
from .optimisation import Formation
from .value_objects import GameweekNumber


def _validate_sha256(value: str | None, field_name: str) -> str | None:
    if value is not None and (
        len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


class DecisionInputProvenance(DomainModel):
    """Exact source references used to make a recommendation without embedding payloads."""

    official_snapshot_reference: str | None = None
    official_snapshot_id: str | None = None
    official_snapshot_sha256: str | None = None
    projection_provider: str = Field(min_length=1)
    projection_source: str = Field(min_length=1)
    projection_artifact_reference: str | None = None
    projection_sha256: str | None = None
    projection_model_version: str = Field(min_length=1)
    projection_generated_at: AwareDatetime
    availability_assessment_reference: str | None = None
    availability_cutoff_at: AwareDatetime | None = None

    @field_validator("official_snapshot_sha256", "projection_sha256")
    @classmethod
    def hashes_are_lowercase_sha256(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_sha256(value, info.field_name or "digest")


class DecisionSelection(DomainModel):
    """Canonical identities for one complete FPL squad and saved lineup."""

    squad_ids: tuple[UUID, ...] = Field(min_length=15, max_length=15)
    starting_xi_ids: tuple[UUID, ...] = Field(min_length=11, max_length=11)
    captain_id: UUID
    vice_captain_id: UUID
    bench_ids: tuple[UUID, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_identity_and_order(self) -> Self:
        squad = set(self.squad_ids)
        starters = set(self.starting_xi_ids)
        bench = set(self.bench_ids)
        if len(squad) != 15 or len(starters) != 11 or len(bench) != 4:
            raise ValueError("decision selection cannot contain duplicate player IDs")
        if starters & bench or starters | bench != squad:
            raise ValueError("starting XI and bench must partition the selected squad")
        if self.captain_id not in starters or self.vice_captain_id not in starters:
            raise ValueError("captain and vice-captain must both start")
        if self.captain_id == self.vice_captain_id:
            raise ValueError("captain and vice-captain must differ")
        if self.squad_ids != tuple(sorted(self.squad_ids, key=str)):
            raise ValueError("squad_ids must use canonical UUID order")
        if self.starting_xi_ids != tuple(sorted(self.starting_xi_ids, key=str)):
            raise ValueError("starting_xi_ids must use canonical UUID order")
        return self

    @property
    def identity(self) -> tuple[object, ...]:
        """Return the saved-choice identity, excluding observation metadata."""

        return (
            self.squad_ids,
            self.starting_xi_ids,
            self.captain_id,
            self.vice_captain_id,
            self.bench_ids,
        )


class DecisionRecommendation(DecisionSelection):
    """Model recommendation and its nominal #6 score at decision time."""

    formation: Formation
    squad_cost_tenths_million: int = Field(ge=0)
    bank_remaining_tenths_million: int = Field(ge=0)
    primary_objective: float
    solver_status: str = Field(min_length=1)

    @field_validator("primary_objective")
    @classmethod
    def objective_must_be_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("primary_objective must be finite")
        return value


class SubmittedDecision(DecisionSelection):
    """The squad and lineup actually saved in FPL, recorded after recommendation."""

    recorded_at: AwareDatetime


class DecisionDeviation(DomainModel):
    """Human reasons for deliberately submitting a choice different from the model."""

    reasons: tuple[str, ...] = Field(min_length=1)

    @field_validator("reasons")
    @classmethod
    def reasons_are_meaningful_and_unique(cls, reasons: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(reason.strip() for reason in reasons)
        if any(not reason for reason in cleaned):
            raise ValueError("deviation reasons must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("deviation reasons must be unique")
        return cleaned


class DecisionBundleV1(DomainModel):
    """Immutable v1 evidence bundle separating recommendation from submitted choice."""

    schema_version: int = 1
    decision_run_id: UUID
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    decision_at: AwareDatetime
    code_revision: str = Field(min_length=1)
    config_fingerprint: str = Field(min_length=1)
    inputs: DecisionInputProvenance
    recommendation: DecisionRecommendation
    actual_choice: SubmittedDecision | None = None
    deviation: DecisionDeviation | None = None

    @field_validator("schema_version")
    @classmethod
    def supports_only_v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported decision bundle schema_version {value}; supported: 1")
        return value

    @model_validator(mode="after")
    def validate_temporal_and_deviation_integrity(self) -> Self:
        if self.inputs.projection_generated_at > self.decision_at:
            raise ValueError("projection_generated_at cannot be after decision_at")
        if (
            self.inputs.availability_cutoff_at is not None
            and self.inputs.availability_cutoff_at > self.decision_at
        ):
            raise ValueError("availability_cutoff_at cannot be after decision_at")
        if self.actual_choice is not None and self.actual_choice.recorded_at < self.decision_at:
            raise ValueError("actual_choice.recorded_at cannot be before decision_at")
        if self.actual_choice is None:
            if self.deviation is not None:
                raise ValueError("deviation requires an actual submitted choice")
            return self
        differs = self.actual_choice.identity != self.recommendation.identity
        if differs and self.deviation is None:
            raise ValueError("a submitted choice differing from recommendation requires a reason")
        if not differs and self.deviation is not None:
            raise ValueError("an identical submitted choice must not record a false deviation")
        return self

    def record_actual_choice(
        self,
        actual_choice: SubmittedDecision,
        *,
        deviation_reasons: tuple[str, ...] = (),
    ) -> DecisionBundleV1:
        """Return a new bundle with submission evidence; the recommendation is retained."""

        differs = actual_choice.identity != self.recommendation.identity
        if differs and not deviation_reasons:
            raise ValueError("a differing actual choice requires at least one deviation reason")
        if not differs and deviation_reasons:
            raise ValueError("identical actual choice cannot have deviation reasons")
        return DecisionBundleV1(
            schema_version=self.schema_version,
            decision_run_id=self.decision_run_id,
            season=self.season,
            gameweek=self.gameweek,
            decision_at=self.decision_at,
            code_revision=self.code_revision,
            config_fingerprint=self.config_fingerprint,
            inputs=self.inputs,
            recommendation=self.recommendation,
            actual_choice=actual_choice,
            deviation=(DecisionDeviation(reasons=deviation_reasons) if differs else None),
        )
