import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import (
    ChipState,
    ChipStatus,
    ChipType,
    GameweekNumber,
    Money,
)


def test_money_is_exact_in_tenths() -> None:
    price = Money.from_millions(7, 5)
    assert price.tenths_million == 75
    assert price.display == "£7.5m"


def test_gameweek_number_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        GameweekNumber(value=39)


def test_used_chip_requires_gameweek() -> None:
    with pytest.raises(ValidationError):
        ChipState(chip_type=ChipType.WILDCARD, half=1, status=ChipStatus.USED)


def test_available_chip_cannot_have_used_gameweek() -> None:
    with pytest.raises(ValidationError):
        ChipState(
            chip_type=ChipType.BENCH_BOOST,
            half=1,
            status=ChipStatus.AVAILABLE,
            used_gameweek=GameweekNumber(value=4),
        )
