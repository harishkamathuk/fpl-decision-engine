"""Solver-independent contracts for single-gameweek transfer decisions."""

from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import DomainModel
from .models import ManagerState, Player, Projection, Squad
from .optimisation import Formation, OptimisationDiagnostic
from .value_objects import GameweekNumber, Money, Position


class TransferPair(DomainModel):
    """Deterministically paired same-position transfer for presentation."""

    player_out_id: UUID
    player_in_id: UUID
    position: Position
    selling_price: Money
    buying_price: Money

    @model_validator(mode="after")
    def players_differ(self) -> Self:
        if self.player_out_id == self.player_in_id:
            raise ValueError("transfer pair players must differ")
        return self


class SingleGameweekTransferOptimisationRequest(DomainModel):
    """Canonical current state and candidate universe for one transfer decision."""

    target_gameweek: GameweekNumber
    manager_state: ManagerState
    players: tuple[Player, ...]
    projections: tuple[Projection, ...]
    max_transfers: int = Field(default=2, ge=0, le=15)
    excluded_players: frozenset[UUID] = frozenset()

    @model_validator(mode="after")
    def gameweek_matches_state(self) -> Self:
        if self.manager_state.gameweek != self.target_gameweek:
            raise ValueError("manager state gameweek must match the transfer target")
        return self


class SingleGameweekTransferOptimisationResult(DomainModel):
    """Complete legal recommendation and incremental normal-transfer accounting.

    Existing manager hit cost is intentionally absent from net gain because it is sunk
    at the decision timestamp. The first bench entry is the reserve goalkeeper.
    """

    transfers: tuple[TransferPair, ...]
    final_squad: Squad
    starting_xi: tuple[UUID, ...] = Field(min_length=11, max_length=11)
    captain_id: UUID
    vice_captain_id: UUID
    bench: tuple[UUID, ...] = Field(min_length=4, max_length=4)
    formation: Formation
    transfer_count: int = Field(ge=0, le=15)
    free_transfers_used: int = Field(ge=0, le=5)
    paid_transfers: int = Field(ge=0, le=15)
    additional_points_cost: int = Field(ge=0)
    bank_before: Money
    bank_after: Money
    next_free_transfers: int = Field(ge=1, le=5)
    gross_expected_score: float
    net_expected_score: float
    do_nothing_expected_score: float
    expected_gain: float
    final_squad_expected_points: float
    solver_name: str = Field(min_length=1)
    solver_status: str = Field(min_length=1)
    runtime_seconds: float = Field(ge=0)
    objective_bound: float | None = None
    mip_gap: float | None = Field(default=None, ge=0)
    diagnostics: tuple[OptimisationDiagnostic, ...] = ()

    @field_validator(
        "gross_expected_score",
        "net_expected_score",
        "do_nothing_expected_score",
        "expected_gain",
        "final_squad_expected_points",
        "runtime_seconds",
        "objective_bound",
        "mip_gap",
    )
    @classmethod
    def finite_numbers(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("transfer result values must be finite")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.transfer_count != len(self.transfers):
            raise ValueError("transfer count must match paired transfers")
        if self.free_transfers_used + self.paid_transfers != self.transfer_count:
            raise ValueError("free and paid transfers must partition transfer count")
        if self.additional_points_cost != 4 * self.paid_transfers:
            raise ValueError("additional points cost must be four per paid transfer")
        if (
            abs(self.net_expected_score - (self.gross_expected_score - self.additional_points_cost))
            > 1e-7
        ):
            raise ValueError("net score must equal gross score less additional hit cost")
        if (
            abs(self.expected_gain - (self.net_expected_score - self.do_nothing_expected_score))
            > 1e-7
        ):
            raise ValueError("expected gain must compare net score with doing nothing")

        members = {member.player_id: member for member in self.final_squad.members}
        starters = set(self.starting_xi)
        bench = set(self.bench)
        if len(starters) != 11 or len(bench) != 4:
            raise ValueError("starting XI and bench cannot contain duplicates")
        if starters & bench or starters | bench != members.keys():
            raise ValueError("starting XI and bench must partition the final squad")
        if self.captain_id not in starters or self.vice_captain_id not in starters:
            raise ValueError("captain and vice-captain must start")
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
        return self
