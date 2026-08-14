"""Canonical FPL domain entities."""

from collections import Counter
from math import isfinite
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .base import DomainModel
from .value_objects import ChipState, ExternalRef, GameweekNumber, Money, Position


class Team(DomainModel):
    id: UUID
    name: str = Field(min_length=1)
    short_name: str = Field(min_length=1, max_length=4)
    external_refs: tuple[ExternalRef, ...] = ()


class Player(DomainModel):
    id: UUID
    team_id: UUID
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    web_name: str = Field(min_length=1)
    position: Position
    price: Money
    active: bool = True
    external_refs: tuple[ExternalRef, ...] = ()


class Gameweek(DomainModel):
    number: GameweekNumber
    name: str = Field(min_length=1)
    deadline_at: AwareDatetime
    finished: bool = False


class Fixture(DomainModel):
    id: UUID
    home_team_id: UUID
    away_team_id: UUID
    kickoff_at: AwareDatetime | None = None
    gameweek: GameweekNumber | None = None
    external_refs: tuple[ExternalRef, ...] = ()

    @model_validator(mode="after")
    def teams_must_differ(self) -> Self:
        if self.home_team_id == self.away_team_id:
            raise ValueError("fixture home and away teams must differ")
        return self


class Projection(DomainModel):
    player_id: UUID
    gameweek: GameweekNumber
    expected_points: float = Field(ge=0)
    expected_minutes: float | None = Field(default=None, ge=0, le=120)
    start_probability: float | None = Field(default=None, ge=0, le=1)
    variance: float | None = Field(default=None, ge=0)
    p10: float | None = Field(default=None, ge=0)
    p50: float | None = Field(default=None, ge=0)
    p90: float | None = Field(default=None, ge=0)
    source: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    generated_at: AwareDatetime

    @field_validator(
        "expected_points",
        "expected_minutes",
        "start_probability",
        "variance",
        "p10",
        "p50",
        "p90",
    )
    @classmethod
    def finite_numbers(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("projection values must be finite")
        return value

    @model_validator(mode="after")
    def percentiles_must_be_ordered(self) -> Self:
        if (
            self.p10 is not None
            and self.p50 is not None
            and self.p90 is not None
            and not self.p10 <= self.p50 <= self.p90
        ):
            raise ValueError("projection percentiles must satisfy p10 <= p50 <= p90")
        return self


class SquadMember(DomainModel):
    player_id: UUID
    team_id: UUID
    position: Position
    purchase_price: Money | None = None


class Squad(DomainModel):
    members: tuple[SquadMember, ...]

    @model_validator(mode="after")
    def validate_squad(self) -> Self:
        if len(self.members) != 15:
            raise ValueError("squad must contain exactly 15 players")

        player_ids = [member.player_id for member in self.members]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("squad cannot contain the same player more than once")

        expected_positions = Counter(
            {
                Position.GOALKEEPER: 2,
                Position.DEFENDER: 5,
                Position.MIDFIELDER: 5,
                Position.FORWARD: 3,
            }
        )
        actual_positions = Counter(member.position for member in self.members)
        if actual_positions != expected_positions:
            raise ValueError("squad must contain 2 GK, 5 DEF, 5 MID and 3 FWD")

        club_counts = Counter(member.team_id for member in self.members)
        if any(count > 3 for count in club_counts.values()):
            raise ValueError("squad cannot contain more than three players from one club")

        return self


class ManagerState(DomainModel):
    manager_id: UUID
    gameweek: GameweekNumber
    squad: Squad
    bank: Money
    free_transfers: int = Field(ge=0, le=5)
    chips: tuple[ChipState, ...] = ()

    @model_validator(mode="after")
    def chips_must_be_unique_per_half(self) -> Self:
        keys = [(chip.chip_type, chip.half) for chip in self.chips]
        if len(set(keys)) != len(keys):
            raise ValueError("manager state cannot contain duplicate chip state for a half")
        return self


class Transfer(DomainModel):
    gameweek: GameweekNumber
    player_out_id: UUID
    player_in_id: UUID
    points_cost: int = Field(default=0, ge=0)
    made_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def players_must_differ(self) -> Self:
        if self.player_out_id == self.player_in_id:
            raise ValueError("transfer in and out players must differ")
        return self


class LeagueEntry(DomainModel):
    manager_id: UUID
    rank: int = Field(ge=1)
    total_points: int = Field(ge=0)


class League(DomainModel):
    id: UUID
    name: str = Field(min_length=1)
    entries: tuple[LeagueEntry, ...] = ()

    @model_validator(mode="after")
    def manager_ids_must_be_unique(self) -> Self:
        manager_ids = [entry.manager_id for entry in self.entries]
        if len(set(manager_ids)) != len(manager_ids):
            raise ValueError("league cannot contain the same manager more than once")
        return self


class DecisionRun(DomainModel):
    id: UUID
    created_at: AwareDatetime
    gameweek: GameweekNumber
    code_revision: str = Field(min_length=1)
    config_fingerprint: str = Field(min_length=1)
    input_snapshot_ids: tuple[str, ...] = ()
    projection_versions: tuple[str, ...] = ()
    random_seed: int | None = None
