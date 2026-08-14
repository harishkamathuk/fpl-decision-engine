"""Value objects used by the canonical FPL domain model."""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from .base import DomainModel


class Position(StrEnum):
    GOALKEEPER = "GK"
    DEFENDER = "DEF"
    MIDFIELDER = "MID"
    FORWARD = "FWD"


class ChipType(StrEnum):
    WILDCARD = "wildcard"
    FREE_HIT = "free_hit"
    BENCH_BOOST = "bench_boost"
    TRIPLE_CAPTAIN = "triple_captain"


class ChipStatus(StrEnum):
    AVAILABLE = "available"
    USED = "used"
    EXPIRED = "expired"


class Money(DomainModel):
    """FPL money represented exactly in tenths of a million pounds."""

    tenths_million: int = Field(ge=0)

    @classmethod
    def from_millions(cls, whole: int, tenths: int = 0) -> Self:
        if whole < 0 or not 0 <= tenths <= 9:
            raise ValueError("money components must be non-negative with tenths between 0 and 9")
        return cls(tenths_million=(whole * 10) + tenths)

    @property
    def display(self) -> str:
        whole, tenths = divmod(self.tenths_million, 10)
        return f"£{whole}.{tenths}m"


class GameweekNumber(DomainModel):
    value: int = Field(ge=1, le=38)


class ExternalRef(DomainModel):
    """Provider-specific identifier kept separate from stable internal IDs."""

    provider: str = Field(min_length=1)
    external_id: str = Field(min_length=1)


class ChipState(DomainModel):
    chip_type: ChipType
    half: int = Field(ge=1, le=2)
    status: ChipStatus = ChipStatus.AVAILABLE
    used_gameweek: GameweekNumber | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        if self.status is ChipStatus.USED and self.used_gameweek is None:
            raise ValueError("used chip must record the gameweek in which it was used")
        if self.status is not ChipStatus.USED and self.used_gameweek is not None:
            raise ValueError("only a used chip may record used_gameweek")
        return self
