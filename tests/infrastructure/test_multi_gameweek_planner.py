from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    GameweekNumber,
    ManagerState,
    Money,
    MultiGameweekPlanningRequest,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
    SingleGameweekTransferOptimisationRequest,
    Squad,
    SquadMember,
)
from fpl_decision_engine.infrastructure.optimisation import (
    HighsMultiGameweekPlanner,
    HighsSingleGameweekOptimiser,
    HighsSingleGameweekTransferOptimiser,
)
from fpl_decision_engine.ports import (
    OptimisationEngine,
    OptimisationError,
    OptimisationErrorCode,
)

GENERATED_AT = datetime(2026, 8, 15, 8, tzinfo=UTC)
QUOTAS = {
    Position.GOALKEEPER: 2,
    Position.DEFENDER: 5,
    Position.MIDFIELDER: 5,
    Position.FORWARD: 3,
}
CANDIDATE_COUNTS = {
    Position.GOALKEEPER: 3,
    Position.DEFENDER: 7,
    Position.MIDFIELDER: 7,
    Position.FORWARD: 5,
}
PERFORMANCE_COUNTS = {
    Position.GOALKEEPER: 8,
    Position.DEFENDER: 20,
    Position.MIDFIELDER: 20,
    Position.FORWARD: 12,
}


def make_request(
    *,
    horizon: int = 3,
    free_transfers: int = 1,
    bank: int = 20,
    discount_factor: float = 1.0,
    gameweek_weights: tuple[float, ...] = (),
    transfer_limits: tuple[int, ...] | None = None,
    point_updates: dict[tuple[UUID, int], float] | None = None,
    player_updates: dict[UUID, dict[str, object]] | None = None,
    member_updates: dict[UUID, dict[str, object]] | None = None,
    existing_points_cost: int = 8,
    candidate_counts: dict[Position, int] | None = None,
) -> MultiGameweekPlanningRequest:
    players: list[Player] = []
    owned_ids: set[UUID] = set()
    number = 1
    counts = candidate_counts or CANDIDATE_COUNTS
    for position in Position:
        for position_index in range(counts[position]):
            player_id = UUID(int=number)
            player = Player(
                id=player_id,
                team_id=UUID(int=10_000 + ((number - 1) % 20)),
                first_name=f"First{number}",
                last_name=f"Last{number}",
                web_name=f"P{number}",
                position=position,
                price=Money(tenths_million=50),
            )
            if player_updates and player_id in player_updates:
                player = player.model_copy(update=player_updates[player_id])
            players.append(player)
            if position_index < QUOTAS[position]:
                owned_ids.add(player_id)
            number += 1

    player_by_id = {player.id: player for player in players}
    members: list[SquadMember] = []
    for player_id in sorted(owned_ids, key=str):
        player = player_by_id[player_id]
        member = SquadMember(
            player_id=player.id,
            team_id=player.team_id,
            position=player.position,
            purchase_price=player.price,
            selling_price=player.price,
        )
        if member_updates and player_id in member_updates:
            member = member.model_copy(update=member_updates[player_id])
        members.append(member)

    projections: list[Projection] = []
    for gameweek in range(1, horizon + 1):
        for player in players:
            base_points = 6.0 - (player.id.int * 0.01) if player.id in owned_ids else 1.0
            points = (
                point_updates.get((player.id, gameweek), base_points)
                if point_updates
                else base_points
            )
            projections.append(
                Projection(
                    player_id=player.id,
                    gameweek=GameweekNumber(value=gameweek),
                    expected_points=points,
                    appearance_probability=0.01,
                    source="synthetic",
                    model_version="v1",
                    generated_at=GENERATED_AT,
                )
            )

    state = ManagerState(
        manager_id=UUID(int=999),
        gameweek=GameweekNumber(value=1),
        squad=Squad(members=tuple(members)),
        bank=Money(tenths_million=bank),
        free_transfers=free_transfers,
        existing_points_cost=existing_points_cost,
    )
    return MultiGameweekPlanningRequest(
        starting_gameweek=GameweekNumber(value=1),
        horizon=horizon,
        initial_manager_state=state,
        players=tuple(players),
        projections=tuple(projections),
        discount_factor=discount_factor,
        gameweek_weights=gameweek_weights,
        max_transfers_per_gameweek=(
            transfer_limits if transfer_limits is not None else (2,) * horizon
        ),
    )


def owned_ids(request: MultiGameweekPlanningRequest) -> set[UUID]:
    return {member.player_id for member in request.initial_manager_state.squad.members}


def outsiders(request: MultiGameweekPlanningRequest, position: Position) -> tuple[Player, ...]:
    current = owned_ids(request)
    return tuple(
        player
        for player in request.players
        if player.position is position and player.id not in current
    )


def assert_legal_week(week) -> None:
    members = {member.player_id: member for member in week.squad.members}
    assert Counter(member.position for member in members.values()) == Counter(QUOTAS)
    assert max(Counter(member.team_id for member in members.values()).values()) <= 3
    assert len(week.starting_xi) == 11
    assert week.captain_id in week.starting_xi
    assert week.vice_captain_id in week.starting_xi
    assert week.captain_id != week.vice_captain_id
    assert members[week.bench[0]].position is Position.GOALKEEPER


def test_horizon_one_matches_single_gameweek_transfer_semantics() -> None:
    base = make_request(horizon=1, transfer_limits=(1,))
    incoming = outsiders(base, Position.MIDFIELDER)[0]
    request = make_request(
        horizon=1,
        transfer_limits=(1,),
        point_updates={(incoming.id, 1): 20.0},
    )
    single_request = SingleGameweekTransferOptimisationRequest(
        target_gameweek=request.starting_gameweek,
        manager_state=request.initial_manager_state,
        players=request.players,
        projections=request.projections,
        max_transfers=1,
    )

    multi = HighsMultiGameweekPlanner().optimise(request)
    single = HighsSingleGameweekTransferOptimiser().optimise(single_request)
    week = multi.actionable_gameweek

    assert week.transfer_out_ids == tuple(item.player_out_id for item in single.transfers)
    assert week.transfer_in_ids == tuple(item.player_in_id for item in single.transfers)
    assert {member.player_id for member in week.squad.members} == {
        member.player_id for member in single.final_squad.members
    }
    assert week.starting_xi == single.starting_xi
    assert week.captain_id == single.captain_id
    assert week.vice_captain_id == single.vice_captain_id
    assert week.bench == single.bench
    assert week.hit_cost == single.additional_points_cost
    assert week.bank_after == single.bank_after
    assert week.net_expected_score == pytest.approx(single.net_expected_score)
    assert multi.weighted_expected_gain == pytest.approx(single.expected_gain)


def test_hold_trajectory_accumulates_free_transfers_and_keeps_bank() -> None:
    request = make_request(
        horizon=6,
        free_transfers=1,
        bank=17,
        transfer_limits=(0,) * 6,
    )
    result = HighsMultiGameweekPlanner().optimise(request)

    assert [item.free_transfers_available for item in result.hold_trajectory] == [
        1,
        2,
        3,
        4,
        5,
        5,
    ]
    assert [item.next_free_transfers for item in result.hold_trajectory] == [
        2,
        3,
        4,
        5,
        5,
        5,
    ]
    assert all(item.transfer_count == item.hit_cost == 0 for item in result.hold_trajectory)
    assert all(
        item.bank_before == item.bank_after == Money(tenths_million=17)
        for item in result.hold_trajectory
    )
    assert all(
        {member.player_id for member in item.squad.members} == owned_ids(request)
        for item in result.hold_trajectory
    )
    assert all(
        item.weighted_contribution == pytest.approx(item.net_expected_score)
        for item in result.hold_trajectory
    )
    for week in result.hold_trajectory:
        assert_legal_week(week)


def test_squad_and_bank_carry_forward_after_first_week_transfer() -> None:
    base = make_request(horizon=3)
    incoming = outsiders(base, Position.FORWARD)[0]
    request = make_request(
        horizon=3,
        transfer_limits=(1, 0, 0),
        point_updates={(incoming.id, gameweek): 20.0 for gameweek in range(1, 4)},
    )
    original_projections = request.projections
    result = HighsMultiGameweekPlanner().optimise(request)

    assert request.projections == original_projections
    assert result.gameweeks[0].transfer_in_ids == (incoming.id,)
    assert all(
        incoming.id in {member.player_id for member in week.squad.members}
        for week in result.gameweeks
    )
    assert result.gameweeks[1].bank_before == result.gameweeks[0].bank_after
    assert result.gameweeks[2].bank_before == result.gameweeks[1].bank_after
    assert result.gameweeks[1].free_transfers_available == result.gameweeks[0].next_free_transfers
    for week in result.gameweeks:
        assert_legal_week(week)


def test_free_transfer_spending_hits_floor_and_existing_cost_is_sunk() -> None:
    base = make_request(horizon=2)
    midfielder = outsiders(base, Position.MIDFIELDER)[0]
    forward = outsiders(base, Position.FORWARD)[0]
    updates = {
        (player_id, gameweek): 20.0
        for player_id in (midfielder.id, forward.id)
        for gameweek in (1, 2)
    }
    request = make_request(
        horizon=2,
        free_transfers=1,
        transfer_limits=(2, 0),
        point_updates=updates,
        existing_points_cost=12,
    )
    result = HighsMultiGameweekPlanner().optimise(request)
    first = result.gameweeks[0]

    assert first.transfer_count == 2
    assert first.free_transfers_used == 1
    assert first.paid_transfers == 1
    assert first.hit_cost == 4
    assert first.next_free_transfers == 1
    assert result.gameweeks[1].free_transfers_available == 1
    assert result.primary_objective == pytest.approx(
        result.total_weighted_gross_score - result.total_weighted_hit_cost
    )
    assert result.total_weighted_hit_cost == pytest.approx(4.0)


def test_spending_one_of_three_free_transfers_carries_three() -> None:
    base = make_request(horizon=2, free_transfers=3)
    incoming = outsiders(base, Position.FORWARD)[0]
    request = make_request(
        horizon=2,
        free_transfers=3,
        transfer_limits=(1, 0),
        point_updates={(incoming.id, gameweek): 20.0 for gameweek in (1, 2)},
    )
    result = HighsMultiGameweekPlanner().optimise(request)

    assert result.gameweeks[0].transfer_count == 1
    assert result.gameweeks[0].paid_transfers == 0
    assert result.gameweeks[0].next_free_transfers == 3
    assert result.gameweeks[1].free_transfers_available == 3


def test_joint_fixture_swing_differs_from_one_week_choice() -> None:
    base = make_request(horizon=3)
    player_a, player_b = outsiders(base, Position.MIDFIELDER)[:2]
    one_week = make_request(
        horizon=1,
        transfer_limits=(1,),
        point_updates={(player_a.id, 1): 14.0, (player_b.id, 1): 10.0},
    )
    three_week = make_request(
        horizon=3,
        transfer_limits=(1, 0, 0),
        point_updates={
            (player_a.id, 1): 14.0,
            (player_a.id, 2): 0.0,
            (player_a.id, 3): 0.0,
            (player_b.id, 1): 10.0,
            (player_b.id, 2): 10.0,
            (player_b.id, 3): 10.0,
        },
    )

    short = HighsMultiGameweekPlanner().optimise(one_week)
    long = HighsMultiGameweekPlanner().optimise(three_week)

    assert short.actionable_gameweek.transfer_in_ids == (player_a.id,)
    assert long.actionable_gameweek.transfer_in_ids == (player_b.id,)


def test_discounting_can_change_the_selected_trajectory() -> None:
    base = make_request(horizon=3)
    player_a, player_b = outsiders(base, Position.MIDFIELDER)[:2]
    updates = {
        (player_a.id, 1): 14.0,
        (player_a.id, 2): 0.0,
        (player_a.id, 3): 0.0,
        (player_b.id, 1): 10.0,
        (player_b.id, 2): 10.0,
        (player_b.id, 3): 10.0,
    }
    current_only = make_request(
        horizon=3,
        discount_factor=0.0,
        transfer_limits=(1, 0, 0),
        point_updates=updates,
    )
    undiscounted = make_request(
        horizon=3,
        discount_factor=1.0,
        transfer_limits=(1, 0, 0),
        point_updates=updates,
    )

    current_result = HighsMultiGameweekPlanner().optimise(current_only)
    future_result = HighsMultiGameweekPlanner().optimise(undiscounted)

    assert current_result.actionable_gameweek.transfer_in_ids == (player_a.id,)
    assert future_result.actionable_gameweek.transfer_in_ids == (player_b.id,)
    assert future_result.primary_objective == pytest.approx(
        sum(item.net_expected_score for item in future_result.gameweeks)
    )


def test_planner_delays_a_future_only_transfer_and_rolls_allowance() -> None:
    base = make_request(horizon=3)
    incoming = outsiders(base, Position.MIDFIELDER)[0]
    current_midfielders = [
        player.id
        for player in base.players
        if player.id in owned_ids(base) and player.position is Position.MIDFIELDER
    ]
    updates = {
        **{
            (player_id, gameweek): 8.0
            for player_id in current_midfielders
            for gameweek in range(1, 4)
        },
        (incoming.id, 1): 0.0,
        (incoming.id, 2): 15.0,
        (incoming.id, 3): 15.0,
    }
    request = make_request(
        horizon=3,
        transfer_limits=(1, 1, 1),
        point_updates=updates,
    )
    result = HighsMultiGameweekPlanner().optimise(request)

    assert result.gameweeks[0].transfer_count == 0
    assert result.gameweeks[0].next_free_transfers == 2
    assert result.gameweeks[1].transfer_in_ids == (incoming.id,)
    assert result.gameweeks[1].free_transfers_available == 2


def test_multiweek_gain_can_justify_hit_that_one_week_rejects() -> None:
    base = make_request(horizon=3, free_transfers=0)
    incoming = outsiders(base, Position.GOALKEEPER)[0]
    owned_forward = next(
        player
        for player in base.players
        if player.id in owned_ids(base) and player.position is Position.FORWARD
    )
    updates = {
        **{(incoming.id, gameweek): 8.0 for gameweek in range(1, 4)},
        **{(owned_forward.id, gameweek): 20.0 for gameweek in range(1, 4)},
    }
    one_week = make_request(
        horizon=1,
        free_transfers=0,
        transfer_limits=(1,),
        point_updates={key: value for key, value in updates.items() if key[1] == 1},
    )
    three_week = make_request(
        horizon=3,
        free_transfers=0,
        transfer_limits=(1, 0, 0),
        point_updates=updates,
    )

    short = HighsMultiGameweekPlanner().optimise(one_week)
    long = HighsMultiGameweekPlanner().optimise(three_week)

    assert short.total_transfers == 0
    assert long.actionable_gameweek.transfer_in_ids == (incoming.id,)
    assert long.actionable_gameweek.hit_cost == 4
    assert long.weighted_expected_gain > 0


def test_frozen_price_for_initial_sale_and_later_purchase_resale() -> None:
    base = make_request(horizon=2)
    player_a, player_b = outsiders(base, Position.MIDFIELDER)[:2]
    member_updates = {
        member.player_id: {
            "purchase_price": Money(tenths_million=46),
            "selling_price": Money(tenths_million=48),
        }
        for member in base.initial_manager_state.squad.members
    }
    request = make_request(
        horizon=2,
        free_transfers=2,
        bank=15,
        transfer_limits=(1, 1),
        player_updates={
            player_a.id: {"price": Money(tenths_million=55)},
            player_b.id: {"price": Money(tenths_million=60)},
        },
        member_updates=member_updates,
        point_updates={
            (player_a.id, 1): 15.0,
            (player_a.id, 2): 0.0,
            (player_b.id, 1): 0.0,
            (player_b.id, 2): 15.0,
        },
    )
    result = HighsMultiGameweekPlanner().optimise(request)

    assert result.gameweeks[0].transfer_in_ids == (player_a.id,)
    assert result.gameweeks[0].transfers[0].selling_price == Money(tenths_million=48)
    assert result.gameweeks[0].bank_after == Money(tenths_million=8)
    assert result.gameweeks[1].transfer_out_ids == (player_a.id,)
    assert result.gameweeks[1].transfer_in_ids == (player_b.id,)
    assert result.gameweeks[1].transfers[0].selling_price == Money(tenths_million=55)
    assert result.gameweeks[1].bank_before == Money(tenths_million=8)
    assert result.gameweeks[1].bank_after == Money(tenths_million=3)


def test_primary_tie_prefers_zero_transfers_before_squad_quality() -> None:
    request = make_request(
        horizon=3,
        transfer_limits=(1, 1, 1),
        point_updates={
            (player.id, gameweek): 5.0
            for player in make_request(horizon=3).players
            for gameweek in range(1, 4)
        },
    )
    result = HighsMultiGameweekPlanner().optimise(request)
    assert result.total_transfers == 0


def test_result_is_deterministic_under_reversed_input_order() -> None:
    base = make_request(horizon=3)
    incoming = outsiders(base, Position.FORWARD)[0]
    request = make_request(
        horizon=3,
        transfer_limits=(1, 1, 1),
        point_updates={(incoming.id, gameweek): 15.0 for gameweek in range(1, 4)},
    )
    reversed_request = request.model_copy(
        update={
            "players": tuple(reversed(request.players)),
            "projections": tuple(reversed(request.projections)),
        }
    )

    first = HighsMultiGameweekPlanner().optimise(request)
    second = HighsMultiGameweekPlanner().optimise(reversed_request)

    assert first.model_copy(update={"runtime_seconds": 0}) == second.model_copy(
        update={"runtime_seconds": 0}
    )


def test_missing_projection_coverage_fails_explicitly() -> None:
    request = make_request(horizon=3)
    incomplete = request.model_copy(update={"projections": request.projections[:-1]})

    with pytest.raises(OptimisationError) as error:
        HighsMultiGameweekPlanner().optimise(incomplete)

    assert error.value.code is OptimisationErrorCode.INVALID_INPUT
    assert "every candidate requires" in str(error.value)


def test_small_independent_two_week_enumeration_matches_highs() -> None:
    base = make_request(horizon=2, free_transfers=0, transfer_limits=(1, 0))
    incoming = outsiders(base, Position.GOALKEEPER)[0]
    keep_ids = owned_ids(base) | {incoming.id}
    request = base.model_copy(
        update={
            "players": tuple(player for player in base.players if player.id in keep_ids),
            "projections": tuple(
                projection for projection in base.projections if projection.player_id in keep_ids
            ),
        }
    )
    players = {player.id: player for player in request.players}
    projections = {
        (projection.player_id, projection.gameweek.value): projection
        for projection in request.projections
    }
    initial = owned_ids(request)
    trajectories: list[tuple[float, int, float]] = []

    def score(ids: set[UUID], gameweek: int) -> tuple[float, float]:
        selected = tuple(players[player_id] for player_id in sorted(ids, key=str))
        budget = Money(tenths_million=sum(player.price.tenths_million for player in selected))
        lineup = HighsSingleGameweekOptimiser().optimise(
            SingleGameweekOptimisationRequest(
                target_gameweek=GameweekNumber(value=gameweek),
                players=selected,
                projections=tuple(projections[(player.id, gameweek)] for player in selected),
                budget=budget,
            )
        )
        return lineup.primary_objective, lineup.secondary_squad_objective

    hold_scores = [score(initial, gameweek) for gameweek in (1, 2)]
    trajectories.append(
        (
            sum(item[0] for item in hold_scores),
            0,
            sum(item[1] for item in hold_scores),
        )
    )
    for player_out in sorted(initial, key=str):
        if players[player_out].position is not Position.GOALKEEPER:
            continue
        final_ids = (initial - {player_out}) | {incoming.id}
        swapped = [score(final_ids, gameweek) for gameweek in (1, 2)]
        trajectories.append(
            (
                sum(item[0] for item in swapped) - 4,
                -1,
                sum(item[1] for item in swapped),
            )
        )

    result = HighsMultiGameweekPlanner().optimise(request)
    oracle = max(trajectories)

    assert result.primary_objective == pytest.approx(oracle[0])
    assert -result.total_transfers == oracle[1]
    assert result.secondary_squad_objective == pytest.approx(oracle[2])


@pytest.mark.parametrize("horizon", [1, 3, 6, 10])
def test_exact_unpruned_planner_reports_performance_diagnostics(horizon: int) -> None:
    request = make_request(
        horizon=horizon,
        transfer_limits=(1,) * horizon,
        candidate_counts=PERFORMANCE_COUNTS,
    )
    result = HighsMultiGameweekPlanner().optimise(request)

    assert result.solver_status == "Optimal"
    assert result.runtime_seconds >= 0
    diagnostic = next(item for item in result.diagnostics if item.code == "joint_model")
    context = dict(diagnostic.context)
    assert int(context["candidates"]) == len(request.players)
    assert int(context["horizon"]) == horizon
    assert int(context["variables"]) > 0
    assert int(context["constraints"]) > 0


def test_multi_gameweek_planner_satisfies_generic_engine_protocol() -> None:
    assert isinstance(HighsMultiGameweekPlanner(), OptimisationEngine)


def test_legacy_selling_value_cannot_reappear_after_repurchase() -> None:
    base = make_request(horizon=3)
    original = next(
        player
        for player in base.players
        if player.id in owned_ids(base) and player.position is Position.MIDFIELDER
    )
    replacement = outsiders(base, Position.MIDFIELDER)[0]
    player_updates = {
        original.id: {"price": Money(tenths_million=54)},
        replacement.id: {"price": Money(tenths_million=54)},
    }
    member_updates = {
        original.id: {
            "purchase_price": Money(tenths_million=50),
            "selling_price": Money(tenths_million=52),
        }
    }

    delayed_sale = make_request(
        horizon=2,
        free_transfers=2,
        bank=2,
        transfer_limits=(0, 1),
        player_updates=player_updates,
        member_updates=member_updates,
        point_updates={
            (original.id, 2): 0.0,
            (replacement.id, 2): 20.0,
        },
    )
    delayed_result = HighsMultiGameweekPlanner().optimise(delayed_sale)

    assert original.id in {member.player_id for member in delayed_result.gameweeks[0].squad.members}
    assert delayed_result.gameweeks[1].transfer_out_ids == (original.id,)
    assert delayed_result.gameweeks[1].transfers[0].selling_price == Money(tenths_million=52)

    lifecycle = make_request(
        horizon=3,
        free_transfers=3,
        bank=2,
        transfer_limits=(1, 1, 1),
        player_updates=player_updates,
        member_updates=member_updates,
        point_updates={
            (original.id, 1): 0.0,
            (original.id, 2): 20.0,
            (original.id, 3): 0.0,
            (replacement.id, 1): 20.0,
            (replacement.id, 2): 0.0,
            (replacement.id, 3): 20.0,
        },
    )
    result = HighsMultiGameweekPlanner().optimise(lifecycle)
    first, second, third = result.gameweeks

    assert first.transfer_out_ids == (original.id,)
    assert first.transfer_in_ids == (replacement.id,)
    assert first.transfers[0].selling_price == Money(tenths_million=52)
    assert first.transfers[0].buying_price == Money(tenths_million=54)
    assert first.bank_before == Money(tenths_million=2)
    assert first.bank_after == Money(tenths_million=0)

    assert second.transfer_out_ids == (replacement.id,)
    assert second.transfer_in_ids == (original.id,)
    assert second.transfers[0].selling_price == Money(tenths_million=54)
    assert second.transfers[0].buying_price == Money(tenths_million=54)
    assert second.bank_after == Money(tenths_million=0)

    assert third.transfer_out_ids == (original.id,)
    assert third.transfer_in_ids == (replacement.id,)
    assert third.transfers[0].selling_price == Money(tenths_million=54)
    assert third.transfers[0].selling_price != Money(tenths_million=52)
    assert third.bank_after == Money(tenths_million=0)


def test_exact_ft_model_matches_all_states_zero_to_five_and_transfers_zero_to_seven() -> None:
    template = make_request(horizon=1)
    initial = owned_ids(template)
    forced_order: list[UUID] = []
    replacement_capacity = {
        Position.GOALKEEPER: 1,
        Position.DEFENDER: 2,
        Position.MIDFIELDER: 2,
        Position.FORWARD: 2,
    }
    for position in Position:
        forced_order.extend(
            sorted(
                (
                    player.id
                    for player in template.players
                    if player.id in initial and player.position is position
                ),
                key=str,
            )[: replacement_capacity[position]]
        )
    assert len(forced_order) == 7

    for free_transfers in range(6):
        for transfer_count in range(8):
            base = make_request(
                horizon=1,
                free_transfers=free_transfers,
                transfer_limits=(transfer_count,),
            )
            request = base.model_copy(
                update={"excluded_players": frozenset(forced_order[:transfer_count])}
            )
            result = HighsMultiGameweekPlanner().optimise(request)
            week = result.actionable_gameweek

            assert week.transfer_count == transfer_count
            assert week.paid_transfers == max(0, transfer_count - free_transfers)
            assert week.next_free_transfers == min(
                5,
                max(1, free_transfers - transfer_count + 1),
            )


def test_ft_state_carries_through_three_distinct_weekly_transfer_counts() -> None:
    base = make_request(horizon=3, free_transfers=0)
    goalkeeper = outsiders(base, Position.GOALKEEPER)[0]
    midfielder = outsiders(base, Position.MIDFIELDER)[0]
    forward = outsiders(base, Position.FORWARD)[0]
    request = make_request(
        horizon=3,
        free_transfers=0,
        transfer_limits=(0, 1, 2),
        point_updates={
            (goalkeeper.id, 2): 20.0,
            (goalkeeper.id, 3): 20.0,
            (midfielder.id, 3): 20.0,
            (forward.id, 3): 20.0,
        },
    )
    result = HighsMultiGameweekPlanner().optimise(request)

    assert [week.transfer_count for week in result.gameweeks] == [0, 1, 2]
    assert [week.free_transfers_available for week in result.gameweeks] == [0, 1, 1]
    assert [week.paid_transfers for week in result.gameweeks] == [0, 0, 1]
    assert [week.next_free_transfers for week in result.gameweeks] == [1, 1, 1]
    assert all(
        current.free_transfers_available == previous.next_free_transfers
        for previous, current in zip(result.gameweeks, result.gameweeks[1:], strict=False)
    )
