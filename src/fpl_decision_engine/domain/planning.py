"""Solver-independent contracts for joint multi-gameweek transfer planning."""

from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import DomainModel
from .models import ManagerState, Player, Projection, Squad
from .optimisation import Formation, OptimisationDiagnostic
from .transfers import TransferPair
from .value_objects import GameweekNumber, Money, Position


class MultiGameweekPlanningRequest(DomainModel):
    """Canonical inputs and explicit strategy settings for a joint horizon solve.

    Prices are frozen at decision-time values. Initially owned players retain their
    manager-specific selling value until first sold; later purchases buy and sell at
    the same canonical current price. Discounting is strategy configuration and never
    mutates canonical projections.
    """

    starting_gameweek: GameweekNumber
    horizon: int = Field(ge=1, le=38)
    initial_manager_state: ManagerState
    players: tuple[Player, ...]
    projections: tuple[Projection, ...]
    discount_factor: float = Field(default=1.0, ge=0, le=1)
    gameweek_weights: tuple[float, ...] = ()
    max_transfers_per_gameweek: tuple[int, ...] = ()
    excluded_players: frozenset[UUID] = frozenset()

    @field_validator("discount_factor")
    @classmethod
    def discount_is_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("discount factor must be finite")
        return value

    @field_validator("gameweek_weights")
    @classmethod
    def weights_are_finite_and_non_negative(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if any(not isfinite(value) or value < 0 for value in values):
            raise ValueError("gameweek weights must be finite and non-negative")
        return values

    @field_validator("max_transfers_per_gameweek")
    @classmethod
    def transfer_limits_are_bounded(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 0 or value > 15 for value in values):
            raise ValueError("per-gameweek transfer limits must be between zero and 15")
        return values

    @model_validator(mode="after")
    def validate_horizon_settings(self) -> Self:
        if self.initial_manager_state.gameweek != self.starting_gameweek:
            raise ValueError("manager state gameweek must match the planning start")
        if self.starting_gameweek.value + self.horizon - 1 > 38:
            raise ValueError("planning horizon extends beyond gameweek 38")
        if self.gameweek_weights and len(self.gameweek_weights) != self.horizon:
            raise ValueError("explicit gameweek weights must match the horizon")
        if self.max_transfers_per_gameweek and len(self.max_transfers_per_gameweek) != self.horizon:
            raise ValueError("per-gameweek transfer limits must match the horizon")
        return self

    @property
    def target_gameweeks(self) -> tuple[GameweekNumber, ...]:
        return tuple(
            GameweekNumber(value=self.starting_gameweek.value + offset)
            for offset in range(self.horizon)
        )

    @property
    def resolved_weights(self) -> tuple[float, ...]:
        if self.gameweek_weights:
            return self.gameweek_weights
        return tuple(self.discount_factor**offset for offset in range(self.horizon))

    @property
    def resolved_transfer_limits(self) -> tuple[int, ...]:
        if self.max_transfers_per_gameweek:
            return self.max_transfers_per_gameweek
        return (2,) * self.horizon


class PlannedGameweek(DomainModel):
    """One legal state transition in an interpretable receding-horizon plan."""

    gameweek: GameweekNumber
    squad: Squad
    starting_xi: tuple[UUID, ...] = Field(min_length=11, max_length=11)
    captain_id: UUID
    vice_captain_id: UUID
    bench: tuple[UUID, ...] = Field(min_length=4, max_length=4)
    formation: Formation
    transfers: tuple[TransferPair, ...]
    transfer_count: int = Field(ge=0, le=15)
    free_transfers_available: int = Field(ge=0, le=5)
    free_transfers_used: int = Field(ge=0, le=5)
    paid_transfers: int = Field(ge=0, le=15)
    hit_cost: int = Field(ge=0)
    bank_before: Money
    bank_after: Money
    gross_expected_score: float
    net_expected_score: float
    discount_weight: float = Field(ge=0)
    weighted_contribution: float
    next_free_transfers: int = Field(ge=1, le=5)
    squad_expected_points: float

    @field_validator(
        "gross_expected_score",
        "net_expected_score",
        "discount_weight",
        "weighted_contribution",
        "squad_expected_points",
    )
    @classmethod
    def scores_are_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("planned gameweek score values must be finite")
        return value

    @model_validator(mode="after")
    def validate_transition_output(self) -> Self:
        if self.transfer_count != len(self.transfers):
            raise ValueError("transfer count must match transfer pairs")
        if self.free_transfers_used + self.paid_transfers != self.transfer_count:
            raise ValueError("free and paid transfers must partition transfer count")
        if self.free_transfers_used != min(self.transfer_count, self.free_transfers_available):
            raise ValueError("free transfers used must consume available allowance first")
        if self.hit_cost != 4 * self.paid_transfers:
            raise ValueError("hit cost must be four points per paid transfer")
        if abs(self.net_expected_score - (self.gross_expected_score - self.hit_cost)) > 1e-7:
            raise ValueError("net score must equal gross score less hit cost")
        if abs(self.weighted_contribution - self.discount_weight * self.net_expected_score) > 1e-7:
            raise ValueError("weighted contribution must equal weight times net score")
        expected_next = min(
            5,
            max(
                1,
                self.free_transfers_available - self.transfer_count + 1,
            ),
        )
        if self.next_free_transfers != expected_next:
            raise ValueError("next free-transfer state does not follow normal rollover")

        members = {member.player_id: member for member in self.squad.members}
        starters = set(self.starting_xi)
        bench = set(self.bench)
        if len(starters) != 11 or len(bench) != 4:
            raise ValueError("starting XI and bench cannot contain duplicates")
        if starters & bench or starters | bench != members.keys():
            raise ValueError("starting XI and bench must partition the squad")
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
            raise ValueError("formation does not match starting XI")
        if members[self.bench[0]].position is not Position.GOALKEEPER:
            raise ValueError("first bench player must be the reserve goalkeeper")
        if any(members[player_id].position is Position.GOALKEEPER for player_id in self.bench[1:]):
            raise ValueError("outfield bench slots cannot contain a goalkeeper")
        return self

    @property
    def transfer_out_ids(self) -> tuple[UUID, ...]:
        return tuple(item.player_out_id for item in self.transfers)

    @property
    def transfer_in_ids(self) -> tuple[UUID, ...]:
        return tuple(item.player_in_id for item in self.transfers)


class MultiGameweekPlanningResult(DomainModel):
    """Complete joint plan; only the first Gameweek move is currently actionable."""

    horizon: int = Field(ge=1, le=38)
    gameweeks: tuple[PlannedGameweek, ...]
    hold_trajectory: tuple[PlannedGameweek, ...]
    total_transfers: int = Field(ge=0)
    total_weighted_gross_score: float
    total_weighted_hit_cost: float = Field(ge=0)
    primary_objective: float
    secondary_squad_objective: float
    hold_baseline_score: float
    weighted_expected_gain: float
    solver_name: str = Field(min_length=1)
    solver_status: str = Field(min_length=1)
    runtime_seconds: float = Field(ge=0)
    objective_bound: float | None = None
    mip_gap: float | None = Field(default=None, ge=0)
    diagnostics: tuple[OptimisationDiagnostic, ...] = ()

    @field_validator(
        "total_weighted_gross_score",
        "total_weighted_hit_cost",
        "primary_objective",
        "secondary_squad_objective",
        "hold_baseline_score",
        "weighted_expected_gain",
        "runtime_seconds",
        "objective_bound",
        "mip_gap",
    )
    @classmethod
    def result_values_are_finite(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("planning result values must be finite")
        return value

    @model_validator(mode="after")
    def validate_trajectory_totals(self) -> Self:
        if len(self.gameweeks) != self.horizon or len(self.hold_trajectory) != self.horizon:
            raise ValueError("planned and hold trajectories must match the horizon")
        if self.total_transfers != sum(item.transfer_count for item in self.gameweeks):
            raise ValueError("total transfers must equal the trajectory transfer count")
        gross = sum(item.discount_weight * item.gross_expected_score for item in self.gameweeks)
        hits = sum(item.discount_weight * item.hit_cost for item in self.gameweeks)
        hold = sum(item.weighted_contribution for item in self.hold_trajectory)
        if abs(self.total_weighted_gross_score - gross) > 1e-7:
            raise ValueError("weighted gross total does not match the trajectory")
        if abs(self.total_weighted_hit_cost - hits) > 1e-7:
            raise ValueError("weighted hit total does not match the trajectory")
        if abs(self.primary_objective - (gross - hits)) > 1e-7:
            raise ValueError("primary objective must equal weighted gross less weighted hits")
        if abs(self.hold_baseline_score - hold) > 1e-7:
            raise ValueError("hold baseline does not match the hold trajectory")
        if abs(self.weighted_expected_gain - (self.primary_objective - hold)) > 1e-7:
            raise ValueError("weighted gain must compare the plan with hold")
        for previous, current in zip(self.gameweeks, self.gameweeks[1:], strict=False):
            if previous.next_free_transfers != current.free_transfers_available:
                raise ValueError("free-transfer state must carry into the next gameweek")
            if previous.bank_after != current.bank_before:
                raise ValueError("bank state must carry into the next gameweek")
            previous_ids = {member.player_id for member in previous.squad.members}
            expected_ids = (previous_ids - set(current.transfer_out_ids)) | set(
                current.transfer_in_ids
            )
            current_ids = {member.player_id for member in current.squad.members}
            if expected_ids != current_ids:
                raise ValueError("squad state does not follow the transfer transition")
        return self

    @property
    def actionable_gameweek(self) -> PlannedGameweek:
        """Return the only transfer decision intended for action before replanning."""

        return self.gameweeks[0]
