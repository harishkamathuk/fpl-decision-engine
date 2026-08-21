"""Post-deadline outcome evidence that may score frozen decisions without altering them."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from fpl_decision_engine.domain.base import DomainModel
from fpl_decision_engine.domain.value_objects import GameweekNumber


class CandidateOutcome(DomainModel):
    """Realised points for one already-preserved candidate selection."""

    decision_run_id: UUID
    squad_ids: tuple[UUID, ...] = Field(min_length=15, max_length=15)
    starting_xi_ids: tuple[UUID, ...] = Field(min_length=11, max_length=11)
    captain_id: UUID
    vice_captain_id: UUID
    bench_ids: tuple[UUID, ...] = Field(min_length=4, max_length=4)
    realised_points: float

    @model_validator(mode="after")
    def validate_identity_partition(self) -> Self:
        squad = set(self.squad_ids)
        starters = set(self.starting_xi_ids)
        bench = set(self.bench_ids)
        if len(squad) != 15 or len(starters) != 11 or len(bench) != 4:
            raise ValueError("candidate outcome cannot contain duplicate player IDs")
        if starters & bench or starters | bench != squad:
            raise ValueError("starting XI and bench must partition the selected squad")
        if self.captain_id not in starters or self.vice_captain_id not in starters:
            raise ValueError("captain and vice-captain must both start")
        if self.captain_id == self.vice_captain_id:
            raise ValueError("captain and vice-captain must differ")
        return self

    @property
    def identity(self) -> tuple[object, ...]:
        """Return the selection identity, matching DecisionSelection.identity."""
        return (
            self.squad_ids,
            self.starting_xi_ids,
            self.captain_id,
            self.vice_captain_id,
            self.bench_ids,
        )


class OutcomeEvidenceV1(DomainModel):
    """Immutable v1 post-deadline outcome evidence for one gameweek.

    This contract is strictly post-deadline and may only be used to score
    already-preserved candidates. It must not be able to change any
    pre-deadline frozen projected values, recommendations, or provenance.
    """

    schema_version: int = 1
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    observed_at: AwareDatetime
    candidates: tuple[CandidateOutcome, ...] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def supports_only_v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError(
                f"unsupported outcome evidence schema_version {value}; supported: 1"
            )
        return value

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        identities = [candidate.identity for candidate in self.candidates]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "outcome evidence cannot contain duplicate candidate selection identities"
            )
        return self

    def outcome_for(self, decision_run_id: UUID) -> CandidateOutcome | None:
        """Return the outcome for a specific decision run, or None if absent."""
        for candidate in self.candidates:
            if candidate.decision_run_id == decision_run_id:
                return candidate
        return None

    def outcome_for_identity(
        self, identity: tuple[object, ...]
    ) -> CandidateOutcome | None:
        """Return the outcome whose selection identity matches exactly."""
        for candidate in self.candidates:
            if candidate.identity == identity:
                return candidate
        return None
