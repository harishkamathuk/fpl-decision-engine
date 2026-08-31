from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

import pytest

from fpl_decision_engine.application import persist_transfer_decision_run
from fpl_decision_engine.domain import (
    GameweekNumber,
    ManagerState,
    Money,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
    SingleGameweekTransferOptimisationRequest,
    Squad,
    SquadMember,
)
from fpl_decision_engine.infrastructure.optimisation import (
    HighsSingleGameweekOptimiser,
    HighsSingleGameweekTransferOptimiser,
)
from fpl_decision_engine.infrastructure.persistence import DuckDbDecisionRunRepository
from fpl_decision_engine.ports import (
    Freshness,
    OptimisationEngine,
    ProviderProvenance,
    ProviderResponse,
)

GAMEWEEK = GameweekNumber(value=1)
GENERATED_AT = datetime(2026, 8, 15, 8, tzinfo=UTC)
QUOTAS = {
    Position.GOALKEEPER: 2,
    Position.DEFENDER: 5,
    Position.MIDFIELDER: 5,
    Position.FORWARD: 3,
}


def make_request(
    *,
    free_transfers: int = 1,
    max_transfers: int = 2,
    bank: int = 20,
    points_updates: dict[UUID, float] | None = None,
    player_updates: dict[UUID, dict[str, object]] | None = None,
    member_updates: dict[UUID, dict[str, object]] | None = None,
    excluded_players: frozenset[UUID] = frozenset(),
) -> SingleGameweekTransferOptimisationRequest:
    counts = {
        Position.GOALKEEPER: 3,
        Position.DEFENDER: 7,
        Position.MIDFIELDER: 7,
        Position.FORWARD: 5,
    }
    players: list[Player] = []
    projections: list[Projection] = []
    owned_ids: set[UUID] = set()
    number = 1
    for position in Position:
        for position_index in range(counts[position]):
            player_id = UUID(int=number)
            player = Player(
                id=player_id,
                team_id=UUID(int=10_000 + ((number - 1) % 8)),
                first_name=f"First{number}",
                last_name=f"Last{number}",
                web_name=f"P{number}",
                position=position,
                price=Money(tenths_million=50),
            )
            if player_updates and player_id in player_updates:
                player = player.model_copy(update=player_updates[player_id])
            players.append(player)
            initially_owned = position_index < QUOTAS[position]
            if initially_owned:
                owned_ids.add(player_id)
            points = 5.0 if initially_owned else 1.0
            if points_updates and player_id in points_updates:
                points = points_updates[player_id]
            projections.append(
                Projection(
                    player_id=player_id,
                    gameweek=GAMEWEEK,
                    expected_points=points,
                    appearance_probability=0.01,
                    source="synthetic",
                    model_version="v1",
                    generated_at=GENERATED_AT,
                )
            )
            number += 1
    player_by_id = {player.id: player for player in players}
    members = []
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
    state = ManagerState(
        manager_id=UUID(int=999),
        gameweek=GAMEWEEK,
        squad=Squad(members=tuple(members)),
        bank=Money(tenths_million=bank),
        free_transfers=free_transfers,
        existing_points_cost=8,
    )
    return SingleGameweekTransferOptimisationRequest(
        target_gameweek=GAMEWEEK,
        manager_state=state,
        players=tuple(players),
        projections=tuple(projections),
        max_transfers=max_transfers,
        excluded_players=excluded_players,
    )


def outside_player(
    request: SingleGameweekTransferOptimisationRequest, position: Position
) -> Player:
    owned = {member.player_id for member in request.manager_state.squad.members}
    return next(
        player
        for player in request.players
        if player.position is position and player.id not in owned
    )


def with_points(
    request: SingleGameweekTransferOptimisationRequest, updates: dict[UUID, float]
) -> SingleGameweekTransferOptimisationRequest:
    return request.model_copy(
        update={
            "projections": tuple(
                projection.model_copy(update={"expected_points": updates[projection.player_id]})
                if projection.player_id in updates
                else projection
                for projection in request.projections
            )
        }
    )


def test_hold_scenario_has_zero_gain_and_rolls_free_transfer() -> None:
    request = make_request(free_transfers=2, max_transfers=0)
    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert result.transfer_count == 0
    assert result.additional_points_cost == 0
    assert result.expected_gain == pytest.approx(0)
    assert result.net_expected_score == pytest.approx(result.do_nothing_expected_score)
    assert result.next_free_transfers == 3


def test_one_free_transfer_has_no_hit_and_returns_legal_plan() -> None:
    base = make_request(free_transfers=1, max_transfers=1)
    incoming = outside_player(base, Position.MIDFIELDER)
    request = with_points(base, {incoming.id: 12.0})

    result = HighsSingleGameweekTransferOptimiser().optimise(request)
    members = {member.player_id: member for member in result.final_squad.members}

    assert result.transfer_count == result.free_transfers_used == 1
    assert result.paid_transfers == result.additional_points_cost == 0
    assert incoming.id in members
    assert Counter(member.position for member in members.values()) == Counter(QUOTAS)
    assert max(Counter(member.team_id for member in members.values()).values()) <= 3
    assert len(result.starting_xi) == 11
    assert result.captain_id in result.starting_xi
    assert result.vice_captain_id in result.starting_xi
    assert result.captain_id != result.vice_captain_id
    assert members[result.bench[0]].position is Position.GOALKEEPER
    assert result.next_free_transfers == 1
    assert result.expected_gain == pytest.approx(14.0)


def test_excess_transfer_costs_exactly_four_points_and_existing_hit_is_sunk() -> None:
    base = make_request(free_transfers=1, max_transfers=2)
    midfielder = outside_player(base, Position.MIDFIELDER)
    forward = outside_player(base, Position.FORWARD)
    request = with_points(base, {midfielder.id: 12.0, forward.id: 12.0})

    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert result.transfer_count == 2
    assert result.free_transfers_used == 1
    assert result.paid_transfers == 1
    assert result.additional_points_cost == 4
    assert result.net_expected_score == pytest.approx(result.gross_expected_score - 4)
    assert result.expected_gain == pytest.approx(
        result.net_expected_score - result.do_nothing_expected_score
    )


def test_paid_transfer_below_hit_value_loses_to_holding() -> None:
    base = make_request(free_transfers=0, max_transfers=1)
    incoming = outside_player(base, Position.GOALKEEPER)
    request = with_points(base, {incoming.id: 6.0})

    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert result.transfer_count == 0
    assert result.expected_gain == pytest.approx(0)


def test_paid_transfer_above_hit_value_may_win() -> None:
    base = make_request(free_transfers=0, max_transfers=1)
    incoming = outside_player(base, Position.GOALKEEPER)
    request = with_points(base, {incoming.id: 9.0})

    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert result.transfer_count == 1
    assert result.paid_transfers == 1
    assert result.additional_points_cost == 4
    assert result.expected_gain > 0


def test_do_nothing_scores_owned_squad_at_current_value_above_initial_budget() -> None:
    initial = make_request(max_transfers=0, bank=0)
    owned_ids = sorted(
        (member.player_id for member in initial.manager_state.squad.members), key=str
    )
    purchase_prices = {
        player_id: (66 if index < 14 else 76) for index, player_id in enumerate(owned_ids)
    }
    player_updates = {
        player_id: {
            "price": Money(tenths_million=purchase_prices[player_id] + (4 if index < 10 else 1))
        }
        for index, player_id in enumerate(owned_ids)
    }
    member_updates = {
        player_id: {
            "purchase_price": Money(tenths_million=purchase_prices[player_id]),
            "selling_price": Money(
                tenths_million=purchase_prices[player_id] + (2 if index < 10 else 0)
            ),
        }
        for index, player_id in enumerate(owned_ids)
    }
    request = make_request(
        max_transfers=0,
        bank=0,
        player_updates=player_updates,
        member_updates=member_updates,
    )

    current_total = sum(
        player.price.tenths_million for player in request.players if player.id in set(owned_ids)
    )
    purchase_total = sum(
        member.purchase_price.tenths_million
        for member in request.manager_state.squad.members
        if member.purchase_price is not None
    )
    selling_total = sum(
        member.selling_price.tenths_million
        for member in request.manager_state.squad.members
        if member.selling_price is not None
    )
    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert (purchase_total, current_total, selling_total) == (1000, 1045, 1020)
    assert result.transfer_count == 0
    assert result.do_nothing_expected_score == pytest.approx(60.0)
    assert result.net_expected_score == pytest.approx(60.0)
    assert result.expected_gain == pytest.approx(0.0)


def test_budget_uses_owned_selling_price_not_market_price() -> None:
    initial = make_request(free_transfers=1, max_transfers=1, bank=0)
    owned_goalkeeper = next(
        member
        for member in initial.manager_state.squad.members
        if member.position is Position.GOALKEEPER
    )
    incoming = outside_player(initial, Position.GOALKEEPER)
    request = make_request(
        free_transfers=1,
        max_transfers=1,
        bank=0,
        points_updates={incoming.id: 20.0},
        player_updates={
            owned_goalkeeper.player_id: {"price": Money(tenths_million=100)},
            incoming.id: {"price": Money(tenths_million=55)},
        },
        member_updates={
            owned_goalkeeper.player_id: {
                "purchase_price": Money(tenths_million=0),
                "selling_price": Money(tenths_million=50),
            }
        },
    )

    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert result.transfer_count == 0
    assert result.bank_after == Money(tenths_million=0)


@pytest.mark.parametrize(
    "free_transfers,expected",
    [(5, 5), (1, 2)],
)
def test_free_transfer_rollover_cap_and_normal_increment(
    free_transfers: int, expected: int
) -> None:
    result = HighsSingleGameweekTransferOptimiser().optimise(
        make_request(free_transfers=free_transfers, max_transfers=0)
    )
    assert result.next_free_transfers == expected


def test_primary_tie_prefers_fewer_transfers_before_bench_quality() -> None:
    base = make_request(free_transfers=1, max_transfers=1)
    incoming = outside_player(base, Position.GOALKEEPER)
    request = with_points(base, {incoming.id: 5.0})

    result = HighsSingleGameweekTransferOptimiser().optimise(request)

    assert result.transfer_count == 0


def test_result_is_deterministic_under_reversed_input_order() -> None:
    base = make_request(free_transfers=1, max_transfers=1)
    incoming = outside_player(base, Position.FORWARD)
    request = with_points(base, {incoming.id: 12.0})
    reversed_request = request.model_copy(
        update={
            "players": tuple(reversed(request.players)),
            "projections": tuple(reversed(request.projections)),
        }
    )

    first = HighsSingleGameweekTransferOptimiser().optimise(request)
    second = HighsSingleGameweekTransferOptimiser().optimise(reversed_request)

    assert first.model_copy(update={"runtime_seconds": 0}) == second.model_copy(
        update={"runtime_seconds": 0}
    )


def test_transfer_optimiser_satisfies_generic_engine_protocol() -> None:
    assert isinstance(HighsSingleGameweekTransferOptimiser(), OptimisationEngine)


def test_small_independent_transfer_enumeration_matches_highs_primary() -> None:
    base = make_request(free_transfers=0, max_transfers=1)
    incoming = outside_player(base, Position.MIDFIELDER)
    request = with_points(base, {incoming.id: 11.0})
    players = {player.id: player for player in request.players}
    projections = {projection.player_id: projection for projection in request.projections}
    owned = {member.player_id for member in request.manager_state.squad.members}
    candidates: list[tuple[float, int, float]] = []

    def score(final_ids: set[UUID], transfer_count: int) -> None:
        final_players = tuple(players[player_id] for player_id in sorted(final_ids, key=str))
        budget = Money(tenths_million=sum(player.price.tenths_million for player in final_players))
        result = HighsSingleGameweekOptimiser().optimise(
            SingleGameweekOptimisationRequest(
                target_gameweek=GAMEWEEK,
                players=final_players,
                projections=tuple(projections[player.id] for player in final_players),
                budget=budget,
                captain_fallback=False,
            )
        )
        net = result.primary_objective - 4 * transfer_count
        candidates.append((net, -transfer_count, result.secondary_squad_objective))

    score(set(owned), 0)
    for player_out_id in sorted(owned, key=str):
        for player_in_id in sorted(players.keys() - owned, key=str):
            if players[player_out_id].position is not players[player_in_id].position:
                continue
            final_ids = (owned - {player_out_id}) | {player_in_id}
            try:
                score(final_ids, 1)
            except Exception:
                continue

    highs = HighsSingleGameweekTransferOptimiser().optimise(request)
    oracle = max(candidates)

    assert highs.net_expected_score == pytest.approx(oracle[0])
    assert -highs.transfer_count == oracle[1]
    assert highs.final_squad_expected_points == pytest.approx(oracle[2])


def test_transfer_recommendation_decision_run_round_trip_preserves_provenance(
    tmp_path,
) -> None:
    base = make_request(max_transfers=1)
    incoming = outside_player(base, Position.MIDFIELDER)
    request = with_points(base, {incoming.id: 12.0})
    result = HighsSingleGameweekTransferOptimiser().optimise(request)
    manager_response = ProviderResponse(
        data=request.manager_state,
        provenance=ProviderProvenance(
            provider_id="local-manager",
            provider_version="1",
            retrieved_at=GENERATED_AT,
            source_reference="manager.json",
            snapshot_id="sha256:manager-state",
            source_sha256="a" * 64,
            mapping_fingerprint="b" * 64,
            season="2026-27",
        ),
        freshness=Freshness(as_of=GENERATED_AT),
    )
    repository = DuckDbDecisionRunRepository(tmp_path / "state" / "fpl.duckdb")
    run_id = UUID(int=50_000)

    run = persist_transfer_decision_run(
        repository,
        run_id=run_id,
        created_at=GENERATED_AT,
        season="2026-27",
        code_revision="deadbeef",
        source_is_dirty=False,
        config_fingerprint="sha256:config",
        manager_response=manager_response,
        request=request,
        result=result,
    )

    assert repository.get(run_id) == run
    assert run.input_snapshot_ids == ("local-manager:sha256:manager-state",)
    assert run.projection_versions == ("synthetic:v1",)
    assert run.optimiser_engine == "highs-single-gameweek-transfers-v1"
    assert run.optimiser_settings == (
        ("free_transfers_remaining", "1"),
        ("hit_cost", "4"),
        ("max_transfers", "1"),
    )
    assert run.output_artifact_references == ()
    assert len(result.transfers) == 1
    assert run.diagnostic_summary is not None
    assert f"transfer_out_ids={result.transfers[0].player_out_id}" in run.diagnostic_summary
    assert f"transfer_in_ids={result.transfers[0].player_in_id}" in run.diagnostic_summary
    assert "transfer_count=1" in run.diagnostic_summary
    assert "incremental_hit=0" in run.diagnostic_summary
    assert "expected_gain=14.000000" in run.diagnostic_summary
    assert "bank_after_tenths_million=20" in run.diagnostic_summary
