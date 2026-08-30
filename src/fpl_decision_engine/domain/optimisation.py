"""Solver-independent contracts for single-gameweek optimisation."""

from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import DomainModel
from .models import Player, Projection, Squad
from .value_objects import GameweekNumber, Money, Position


class Formation(DomainModel):
    """Outfield shape of a legal starting XI; the goalkeeper is implicit."""

    defenders: int = Field(ge=3, le=5)
    midfielders: int = Field(ge=2, le=5)
    forwards: int = Field(ge=1, le=3)

    @model_validator(mode="after")
    def has_ten_outfield_starters(self) -> Self:
        if self.defenders + self.midfielders + self.forwards != 10:
            raise ValueError("formation must contain ten outfield starters")
        return self

    @property
    def label(self) -> str:
        return f"{self.defenders}-{self.midfielders}-{self.forwards}"


class OptimisationDiagnostic(DomainModel):
    """Machine-readable context accompanying solver outcomes and failures."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    context: tuple[tuple[str, str], ...] = ()


class SingleGameweekOptimisationRequest(DomainModel):
    """Canonical candidates and bounded scenarios for one independent solve.

    Budget remains exact integer tenths of a million pounds. Candidate activity is
    deliberately not interpreted as availability: callers express that policy through
    projections or explicit exclusions.
    """

    target_gameweek: GameweekNumber
    players: tuple[Player, ...]
    projections: tuple[Projection, ...]
    budget: Money = Money(tenths_million=1000)
    captain_fallback: bool = True
    must_include_in_squad: frozenset[UUID] = frozenset()
    excluded_players: frozenset[UUID] = frozenset()
    forced_starters: frozenset[UUID] = frozenset()
    forced_captain: UUID | None = None
    forced_vice_captain: UUID | None = None


class SingleGameweekOptimisationResult(DomainModel):
    """Complete solver-independent recommendation and primary-solve diagnostics.

    The first bench entry is the reserve goalkeeper; the remaining entries are
    outfield substitutes in substitution priority order. ``primary_objective`` is the
    nominal XI-plus-captain score, including captain-to-vice fallback value when
    enabled and available, never the secondary squad-quality objective.
    """

    squad: Squad
    starting_xi: tuple[UUID, ...] = Field(min_length=11, max_length=11)
    captain_id: UUID
    vice_captain_id: UUID
    bench: tuple[UUID, ...] = Field(min_length=4, max_length=4)
    formation: Formation
    squad_cost: Money
    bank_remaining: Money
    primary_objective: float
    secondary_squad_objective: float
    solver_name: str = Field(min_length=1)
    solver_status: str = Field(min_length=1)
    runtime_seconds: float = Field(ge=0)
    objective_bound: float | None = None
    mip_gap: float | None = Field(default=None, ge=0)
    diagnostics: tuple[OptimisationDiagnostic, ...] = ()

    @field_validator(
        "primary_objective",
        "secondary_squad_objective",
        "runtime_seconds",
        "objective_bound",
        "mip_gap",
    )
    @classmethod
    def finite_solver_numbers(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("solver result values must be finite")
        return value

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        members = {member.player_id: member for member in self.squad.members}
        starters = set(self.starting_xi)
        bench = set(self.bench)
        if len(starters) != 11:
            raise ValueError("starting XI cannot contain duplicate players")
        if len(bench) != 4:
            raise ValueError("bench cannot contain duplicate players")
        if starters & bench or starters | bench != members.keys():
            raise ValueError("starting XI and bench must partition the squad")
        if self.captain_id not in starters:
            raise ValueError("captain must be in the starting XI")
        if self.vice_captain_id not in starters:
            raise ValueError("vice-captain must be in the starting XI")
        if self.captain_id == self.vice_captain_id:
            raise ValueError("captain and vice-captain must differ")

        positions = Counter(members[player_id].position for player_id in starters)
        if positions[Position.GOALKEEPER] != 1:
            raise ValueError("starting XI must contain exactly one goalkeeper")
        if (
            positions[Position.DEFENDER] != self.formation.defenders
            or positions[Position.MIDFIELDER] != self.formation.midfielders
            or positions[Position.FORWARD] != self.formation.forwards
        ):
            raise ValueError("formation does not match starting XI positions")
        if members[self.bench[0]].position is not Position.GOALKEEPER:
            raise ValueError("first bench player must be the reserve goalkeeper")
        if any(members[player_id].position is Position.GOALKEEPER for player_id in self.bench[1:]):
            raise ValueError("outfield bench slots cannot contain a goalkeeper")

        purchase_prices = [member.purchase_price for member in self.squad.members]
        if any(price is None for price in purchase_prices):
            raise ValueError("optimised squad members must carry their selected price")
        total = sum(price.tenths_million for price in purchase_prices if price is not None)
        if total != self.squad_cost.tenths_million:
            raise ValueError("squad cost does not match member prices")
        return self
