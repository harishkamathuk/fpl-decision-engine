from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import (
    Fixture,
    GameweekNumber,
    League,
    LeagueEntry,
    Position,
    Projection,
    Squad,
    SquadMember,
    Transfer,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def valid_squad_members() -> tuple[SquadMember, ...]:
    positions = (
        [Position.GOALKEEPER] * 2
        + [Position.DEFENDER] * 5
        + [Position.MIDFIELDER] * 5
        + [Position.FORWARD] * 3
    )
    return tuple(
        SquadMember(player_id=uid(index), team_id=uid(100 + ((index - 1) // 3)), position=position)
        for index, position in enumerate(positions, start=1)
    )


def test_valid_squad_enforces_canonical_composition() -> None:
    squad = Squad(members=valid_squad_members())
    assert len(squad.members) == 15


def test_squad_rejects_more_than_three_players_from_one_club() -> None:
    members = list(valid_squad_members())
    members[3] = members[3].model_copy(update={"team_id": members[0].team_id})
    with pytest.raises(ValidationError, match="more than three players"):
        Squad(members=tuple(members))


def test_squad_rejects_wrong_position_counts() -> None:
    members = list(valid_squad_members())
    members[-1] = members[-1].model_copy(update={"position": Position.MIDFIELDER})
    with pytest.raises(ValidationError, match="2 GK, 5 DEF, 5 MID and 3 FWD"):
        Squad(members=tuple(members))


def test_fixture_rejects_same_home_and_away_team() -> None:
    with pytest.raises(ValidationError, match="home and away teams must differ"):
        Fixture(id=uid(1), home_team_id=uid(2), away_team_id=uid(2))


def test_projection_rejects_unordered_percentiles() -> None:
    with pytest.raises(ValidationError, match="p10 <= p50 <= p90"):
        Projection(
            player_id=uid(1),
            gameweek=GameweekNumber(value=1),
            expected_points=6.2,
            p10=6.0,
            p50=5.0,
            p90=10.0,
            source="fixture",
            model_version="1",
            generated_at=datetime.now(UTC),
        )


def test_transfer_rejects_same_player_in_and_out() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        Transfer(gameweek=GameweekNumber(value=1), player_out_id=uid(1), player_in_id=uid(1))


def test_league_rejects_duplicate_manager() -> None:
    entry = LeagueEntry(manager_id=uid(1), rank=1, total_points=100)
    with pytest.raises(ValidationError, match="same manager"):
        League(id=uid(99), name="Test", entries=(entry, entry))
