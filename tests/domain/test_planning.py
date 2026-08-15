from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import (
    GameweekNumber,
    ManagerState,
    Money,
    MultiGameweekPlanningRequest,
    Player,
    Position,
    Projection,
    Squad,
    SquadMember,
)


def minimal_request(**updates: object) -> MultiGameweekPlanningRequest:
    positions = (
        [Position.GOALKEEPER] * 2
        + [Position.DEFENDER] * 5
        + [Position.MIDFIELDER] * 5
        + [Position.FORWARD] * 3
    )
    players = tuple(
        Player(
            id=UUID(int=index),
            team_id=UUID(int=100 + index // 3),
            first_name="First",
            last_name=f"Last{index}",
            web_name=f"P{index}",
            position=position,
            price=Money(tenths_million=50),
        )
        for index, position in enumerate(positions, start=1)
    )
    squad = Squad(
        members=tuple(
            SquadMember(
                player_id=player.id,
                team_id=player.team_id,
                position=player.position,
                purchase_price=player.price,
                selling_price=player.price,
            )
            for player in players
        )
    )
    values = {
        "starting_gameweek": GameweekNumber(value=1),
        "horizon": 1,
        "initial_manager_state": ManagerState(
            manager_id=UUID(int=99),
            gameweek=GameweekNumber(value=1),
            squad=squad,
            bank=Money(tenths_million=0),
            free_transfers=1,
        ),
        "players": players,
        "projections": tuple(
            Projection(
                player_id=player.id,
                gameweek=GameweekNumber(value=1),
                expected_points=5,
                source="synthetic",
                model_version="v1",
                generated_at=datetime(2026, 8, 15, tzinfo=UTC),
            )
            for player in players
        ),
    }
    values.update(updates)
    return MultiGameweekPlanningRequest.model_validate(values)


def test_planning_request_resolves_geometric_weights_and_default_limits() -> None:
    request = minimal_request(discount_factor=0.9)
    assert request.resolved_weights == (1.0,)
    assert request.resolved_transfer_limits == (2,)


def test_planning_request_rejects_horizon_beyond_gameweek_38() -> None:
    base = minimal_request()
    state = base.initial_manager_state.model_copy(update={"gameweek": GameweekNumber(value=38)})
    with pytest.raises(ValidationError, match="beyond gameweek 38"):
        minimal_request(
            starting_gameweek=GameweekNumber(value=38),
            horizon=2,
            initial_manager_state=state,
        )
