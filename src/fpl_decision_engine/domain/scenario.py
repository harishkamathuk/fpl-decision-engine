"""Canonical scenario definitions for bounded optimiser counterfactuals."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .base import DomainModel


class ScenarioConstraints(DomainModel):
    """Supported optimiser constraints expressed only with canonical player IDs.

    Captains and vice-captains are semantically required to start and belong to the
    squad. Those implications are exposed by ``required_starters`` and
    ``required_in_squad`` without requiring callers to repeat them in the declared
    constraint sets.
    """

    must_include: frozenset[UUID] = frozenset()
    excluded: frozenset[UUID] = frozenset()
    forced_starters: frozenset[UUID] = frozenset()
    forced_captain: UUID | None = None
    forced_vice_captain: UUID | None = None

    @property
    def required_starters(self) -> frozenset[UUID]:
        """Return explicit starters plus players forced into a leadership role."""

        return self.forced_starters | frozenset(
            player_id
            for player_id in (self.forced_captain, self.forced_vice_captain)
            if player_id is not None
        )

    @property
    def required_in_squad(self) -> frozenset[UUID]:
        """Return explicit squad inclusions plus all semantically implied inclusions."""

        return self.must_include | self.required_starters

    @model_validator(mode="after")
    def reject_direct_contradictions(self) -> Self:
        if overlap := self.must_include & self.excluded:
            raise ValueError(
                "must_include and excluded contradict for player "
                f"{min(overlap, key=str)}"
            )
        if overlap := self.forced_starters & self.excluded:
            raise ValueError(
                "forced_starters and excluded contradict for player "
                f"{min(overlap, key=str)}"
            )
        if self.forced_captain is not None and self.forced_captain in self.excluded:
            raise ValueError(
                "forced_captain and excluded contradict for player "
                f"{self.forced_captain}"
            )
        if self.forced_vice_captain is not None and self.forced_vice_captain in self.excluded:
            raise ValueError(
                "forced_vice_captain and excluded contradict for player "
                f"{self.forced_vice_captain}"
            )
        if (
            self.forced_captain is not None
            and self.forced_captain == self.forced_vice_captain
        ):
            raise ValueError(
                "forced_captain and forced_vice_captain contradict for player "
                f"{self.forced_captain}"
            )
        return self


class ScenarioDefinition(DomainModel):
    """Named re-optimisation counterfactual with supported deterministic constraints.

    This definition restricts a later optimiser solution space; it never carries or
    modifies projections, forecast provenance, or objective values.
    """

    scenario_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    constraints: ScenarioConstraints
    description: str | None = Field(default=None, min_length=1)
    rationale: str | None = Field(default=None, min_length=1)
    created_at: AwareDatetime | None = None

    @field_validator("scenario_id", "label", "description", "rationale")
    @classmethod
    def text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("scenario text fields must not be blank")
        return value
