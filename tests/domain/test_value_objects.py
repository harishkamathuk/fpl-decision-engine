import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import (
    ChipState,
    ChipStatus,
    ChipType,
    GameweekNumber,
    Money,
    calculate_selling_price,
)


def test_money_is_exact_in_tenths() -> None:
    price = Money.from_millions(7, 5)
    assert price.tenths_million == 75
    assert price.display == "£7.5m"


@pytest.mark.parametrize(
    "purchase,current,expected",
    [
        (75, 78, 76),
        (50, 54, 52),
        (50, 53, 51),
        (50, 51, 50),
        (50, 47, 47),
    ],
)
def test_official_selling_price_uses_half_profit_floor(
    purchase: int, current: int, expected: int
) -> None:
    assert calculate_selling_price(
        purchase=Money(tenths_million=purchase),
        current=Money(tenths_million=current),
    ) == Money(tenths_million=expected)


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
