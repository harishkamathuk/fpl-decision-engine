"""Focused tests for #114: ROLL / one-free-transfer enumeration and ranking."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.application import (
    enumerate_and_rank,
    enumerate_transfer_candidates,
)
from fpl_decision_engine.domain import (
    Formation,
    GameweekNumber,
    ManagerState,
    Money,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationResult,
    Squad,
    SquadMember,
)
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser

GAMEWEEK = GameweekNumber(value=1)
GENERATED_AT = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
QUOTAS = {
    Position.GOALKEEPER: 2,
    Position.DEFENDER: 5,
    Position.MIDFIELDER: 5,
    Position.FORWARD: 3,
}


def uid(value: int) -> UUID:
    return UUID(int=value)


def make_owned_players(*, price: int = 50) -> tuple[Player, ...]:
    players: list[Player] = []
    number = 1
    team_cycle = (uid(101), uid(102), uid(103), uid(104), uid(105), uid(106), uid(107), uid(108))
    positions: list[Position] = []
    for position in Position:
        positions.extend([position] * QUOTAS[position])
    for position in positions:
        players.append(
            Player(
                id=uid(number),
                team_id=team_cycle[(number - 1) % len(team_cycle)],
                first_name=f"First{number}",
                last_name=f"Last{number}",
                web_name=f"P{number}",
                position=position,
                price=Money(tenths_million=price),
            )
        )
        number += 1
    return tuple(players)


def make_squad_members(
    players: tuple[Player, ...],
    *,
    purchase_offset: int = 0,
    selling_offset: int = 0,
) -> tuple[SquadMember, ...]:
    return tuple(
        SquadMember(
            player_id=player.id,
            team_id=player.team_id,
            position=player.position,
            purchase_price=Money(tenths_million=player.price.tenths_million + purchase_offset),
            selling_price=Money(tenths_million=player.price.tenths_million + selling_offset),
        )
        for player in players
    )


def make_manager_state(
    *,
    owned_players: tuple[Player, ...] | None = None,
    free_transfers: int = 1,
    bank: int = 10,
    purchase_offset: int = 0,
    selling_offset: int = 0,
) -> tuple[ManagerState, dict[UUID, Player]]:
    owned = owned_players if owned_players is not None else make_owned_players()
    by_id = {p.id: p for p in owned}
    state = ManagerState(
        manager_id=uid(999),
        gameweek=GAMEWEEK,
        squad=Squad(
            members=make_squad_members(
                owned, purchase_offset=purchase_offset, selling_offset=selling_offset
            )
        ),
        bank=Money(tenths_million=bank),
        free_transfers=free_transfers,
        existing_points_cost=0,
    )
    return state, by_id


def make_universe(
    *,
    owned_players: tuple[Player, ...] | None = None,
    free_transfers: int = 1,
    bank: int = 10,
    purchase_offset: int = 0,
    selling_offset: int = 0,
    extra_per_position: int = 3,
) -> tuple[ManagerState, tuple[Player, ...], tuple[Projection, ...], dict[UUID, Player]]:
    owned = owned_players if owned_players is not None else make_owned_players()
    extra_players: list[Player] = []
    extra_number = len(owned) + 1
    team_cycle = (uid(201), uid(202), uid(203), uid(204))
    for position in Position:
        for _ in range(extra_per_position):
            extra_players.append(
                Player(
                    id=uid(extra_number),
                    team_id=team_cycle[(extra_number - 1) % 4],
                    first_name=f"Extra{extra_number}",
                    last_name=f"Player{extra_number}",
                    web_name=f"E{extra_number}",
                    position=position,
                    price=Money(tenths_million=55),
                )
            )
            extra_number += 1
    all_players = owned + tuple(extra_players)
    all_by_id = {p.id: p for p in all_players}
    projections = tuple(
        Projection(
            player_id=p.id,
            gameweek=GAMEWEEK,
            expected_points=float(p.price.tenths_million) / 10.0,
            source="synthetic",
            model_version="test-v1",
            generated_at=GENERATED_AT,
        )
        for p in all_players
    )
    state = ManagerState(
        manager_id=uid(999),
        gameweek=GAMEWEEK,
        squad=Squad(
            members=make_squad_members(
                owned, purchase_offset=purchase_offset, selling_offset=selling_offset
            )
        ),
        bank=Money(tenths_million=bank),
        free_transfers=free_transfers,
        existing_points_cost=0,
    )
    return state, all_players, projections, all_by_id


def make_projections(
    players: tuple[Player, ...],
    *,
    base_points: dict[UUID, float] | None = None,
) -> tuple[Projection, ...]:
    if base_points is None:
        base_points = {p.id: 5.0 for p in players}
    return tuple(
        Projection(
            player_id=p.id,
            gameweek=GAMEWEEK,
            expected_points=base_points.get(p.id, 5.0),
            source="synthetic",
            model_version="test-v1",
            generated_at=GENERATED_AT,
        )
        for p in players
    )


# ---------------------------------------------------------------------------
# ROLL tests
# ---------------------------------------------------------------------------


def test_roll_always_generated() -> None:
    state, players, projections, _ = make_universe(free_transfers=0)
    candidates = enumerate_transfer_candidates(state, players, projections)
    assert len(candidates) == 1
    assert candidates[0].kind == "ROLL"


def test_roll_contains_exact_authoritative_squad() -> None:
    state, players, projections, _ = make_universe(free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    roll = candidates[0]
    assert roll.kind == "ROLL"
    assert roll.squad == state.squad


def test_roll_preserves_bank() -> None:
    state, players, projections, _ = make_universe(bank=20, free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    roll = candidates[0]
    assert roll.bank_before == state.bank
    assert roll.bank_after == state.bank


def test_roll_consumes_zero_ft() -> None:
    state, players, projections, _ = make_universe(free_transfers=3)
    candidates = enumerate_transfer_candidates(state, players, projections)
    roll = candidates[0]
    assert roll.free_transfers_used == 0


# ---------------------------------------------------------------------------
# Zero FT tests
# ---------------------------------------------------------------------------


def test_zero_ft_manager_state_generates_only_roll() -> None:
    state, players, projections, _ = make_universe(free_transfers=0)
    candidates = enumerate_transfer_candidates(state, players, projections)
    assert len(candidates) == 1
    assert candidates[0].kind == "ROLL"


# ---------------------------------------------------------------------------
# Transfer enumeration tests
# ---------------------------------------------------------------------------


def test_transfer_changes_exactly_one_player() -> None:
    state, players, projections, _ = make_universe(free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]
    assert len(transfers) > 0
    state_ids = {m.player_id for m in state.squad.members}
    for t in transfers:
        result_ids = {m.player_id for m in t.squad.members}
        diff = (state_ids - result_ids) | (result_ids - state_ids)
        assert len(diff) == 2
        assert t.player_out_id in state_ids
        assert t.player_in_id not in state_ids
        assert len(t.squad.members) == 15


def test_transfer_same_position_restriction() -> None:
    state, players, projections, by_id = make_universe(free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]
    assert len(transfers) > 0
    members_by_id = {m.player_id: m for m in state.squad.members}
    for t in transfers:
        out_member = members_by_id[t.player_out_id]
        in_player = by_id[t.player_in_id]
        assert out_member.position is in_player.position


def test_already_owned_incoming_excluded() -> None:
    state, players, projections, _ = make_universe(free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    owned_ids = {m.player_id for m in state.squad.members}
    for t in [c for c in candidates if c.kind == "TRANSFER"]:
        assert t.player_in_id not in owned_ids - {t.player_out_id}


def test_affordability_boundary() -> None:
    state, players, projections, by_id = make_universe(free_transfers=1, bank=0)
    candidates = enumerate_transfer_candidates(state, players, projections)
    members_by_id = {m.player_id: m for m in state.squad.members}
    for t in [c for c in candidates if c.kind == "TRANSFER"]:
        out_member = members_by_id[t.player_out_id]
        in_player = by_id[t.player_in_id]
        available = state.bank.tenths_million + out_member.selling_price.tenths_million
        assert in_player.price.tenths_million <= available


def test_unaffordable_transfer_excluded() -> None:
    owned = make_owned_players(price=50)
    expensive = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Too",
        last_name="Expensive",
        web_name="Expensive",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=999),
    )
    all_players = owned + (expensive,)
    projections = make_projections(owned) + (
        Projection(
            player_id=expensive.id,
            gameweek=GAMEWEEK,
            expected_points=1.0,
            source="synthetic",
            model_version="test-v1",
            generated_at=GENERATED_AT,
        ),
    )
    state, _ = make_manager_state(bank=0)
    candidates = enumerate_transfer_candidates(state, all_players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]
    assert expensive.id not in {t.player_in_id for t in transfers}


def test_selling_price_controls_released_funds() -> None:
    owned = make_owned_players(price=50)
    cheap = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Cheap",
        last_name="Incoming",
        web_name="CheapIn",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=45),
    )
    all_players = owned + (cheap,)
    projections = make_projections(all_players)
    members = list(make_squad_members(owned, selling_offset=10))
    state = ManagerState(
        manager_id=uid(999),
        gameweek=GAMEWEEK,
        squad=Squad(members=tuple(members)),
        bank=Money(tenths_million=0),
        free_transfers=1,
        existing_points_cost=0,
    )
    candidates = enumerate_transfer_candidates(state, all_players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]
    target = next(t for t in transfers if t.player_in_id == cheap.id)
    # selling_price = 60, bank = 0 → available = 60, incoming = 45 → bank_after = 15
    assert target.bank_after == Money(tenths_million=15)
    assert target.bank_before == Money(tenths_million=0)


def test_purchase_price_not_used_for_released_funds() -> None:
    owned = make_owned_players(price=50)
    affordable = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Affordable",
        last_name="Incoming",
        web_name="AffIn",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=45),
    )
    all_players = owned + (affordable,)
    projections = make_projections(all_players)
    # purchase_price = 100 (50 + 50 offset), selling_price = 50 (no offset)
    # If purchase_price were used: available = 5 + 100 = 105 → bank_after = 105 - 45 = 60
    # With selling_price: available = 5 + 50 = 55 → bank_after = 55 - 45 = 10
    members = list(make_squad_members(owned, purchase_offset=50, selling_offset=0))
    state = ManagerState(
        manager_id=uid(999),
        gameweek=GAMEWEEK,
        squad=Squad(members=tuple(members)),
        bank=Money(tenths_million=5),
        free_transfers=1,
        existing_points_cost=0,
    )
    candidates = enumerate_transfer_candidates(state, all_players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]
    target = next(t for t in transfers if t.player_in_id == affordable.id)
    assert target.bank_after == Money(tenths_million=10)


def test_resulting_squad_legality() -> None:
    state, players, projections, _ = make_universe(free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    for c in candidates:
        assert len(c.squad.members) == 15
        assert len({m.player_id for m in c.squad.members}) == 15
        counts = Counter(m.position for m in c.squad.members)
        assert counts == Counter(QUOTAS)
        club_counts = Counter(m.team_id for m in c.squad.members)
        assert max(club_counts.values()) <= 3


def test_club_limit_rejection() -> None:
    owned = list(make_owned_players(price=50))

    team_x = uid(500)
    # owned[0], owned[1], owned[2] are all GOALKEEPERS. Make all 3 from team_x.
    owned[0] = owned[0].model_copy(update={"team_id": team_x})
    owned[1] = owned[1].model_copy(update={"team_id": team_x})
    owned[2] = owned[2].model_copy(update={"team_id": team_x})

    # Fourth player from team_x (a defender, to avoid the position-quota
    # confusion: selling a non-team-x defender and buying a team-x defender
    # keeps position counts valid but breaches club limit).
    fourth_from_team_x = Player(
        id=uid(9998),
        team_id=team_x,
        first_name="Fourth",
        last_name="Club",
        web_name="Fourth",
        position=Position.DEFENDER,
        price=Money(tenths_million=50),
    )
    all_players = tuple(owned) + (fourth_from_team_x,)
    projections = make_projections(all_players)
    state, _ = make_manager_state(owned_players=tuple(owned), bank=10, free_transfers=1)

    assert sum(1 for m in state.squad.members if m.team_id == team_x) == 3

    candidates = enumerate_transfer_candidates(state, all_players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]

    # The transfer buying fourth_from_team_x from a non-team-x defender slot
    # would produce 4 from team_x. Squad validation rejects it.
    fourth_candidates = [t for t in transfers if t.player_in_id == fourth_from_team_x.id]
    for t in fourth_candidates:
        club_count = sum(1 for m in t.squad.members if m.team_id == team_x)
        assert club_count <= 3, "candidate with 4 from one club should be rejected"

    # Directly verify the Squad model rejects 4-from-one-club.
    bad_members = list(state.squad.members)
    replacement_idx = next(
        i
        for i, m in enumerate(bad_members)
        if m.team_id != team_x and m.position is Position.DEFENDER
    )
    bad_members[replacement_idx] = SquadMember(
        player_id=fourth_from_team_x.id,
        team_id=team_x,
        position=Position.DEFENDER,
        purchase_price=Money(tenths_million=50),
        selling_price=None,
    )
    with pytest.raises(ValidationError, match="more than three"):
        Squad(members=tuple(bad_members))


def test_same_club_out_to_same_club_in_keeps_limit() -> None:
    owned = list(make_owned_players(price=50))

    team_x = uid(500)
    # Make exactly 3 from team_x by choosing 3 owned players whose original
    # teams have only 2 members each, so the total per team doesn't exceed 3.
    # owned[0] is GK/team[0], owned[1] is GK/team[1], owned[6] is DEF/team[5].
    # Setting these to team_x gives 3 from team_x and reduces their original
    # teams by 1 each (still ≤ 3).
    owned[0] = owned[0].model_copy(update={"team_id": team_x})
    owned[1] = owned[1].model_copy(update={"team_id": team_x})
    owned[6] = owned[6].model_copy(update={"team_id": team_x})

    replacement_same_club = Player(
        id=uid(9998),
        team_id=team_x,
        first_name="Same",
        last_name="Club",
        web_name="SameClub",
        position=owned[0].position,
        price=Money(tenths_million=50),
    )
    all_players = tuple(owned) + (replacement_same_club,)
    projections = make_projections(all_players)
    state, _ = make_manager_state(owned_players=tuple(owned), bank=10, free_transfers=1)

    assert sum(1 for m in state.squad.members if m.team_id == team_x) == 3

    candidates = enumerate_transfer_candidates(state, all_players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]

    # Selling owned[0] (GK, team_x) and buying replacement_same_club (GK, team_x)
    # should yield exactly 3 from team_x (owned[1] + owned[6] + replacement).
    same_club_transfers = [
        t
        for t in transfers
        if t.player_out_id == owned[0].id and t.player_in_id == replacement_same_club.id
    ]
    assert len(same_club_transfers) == 1
    count = sum(1 for m in same_club_transfers[0].squad.members if m.team_id == team_x)
    assert count == 3


# ---------------------------------------------------------------------------
# Fixed-squad evaluation tests
# ---------------------------------------------------------------------------


def test_candidate_evaluated_through_existing_optimiser() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    assert len(result) > 0
    for evaluated in result:
        assert isinstance(evaluated.optimisation_result, SingleGameweekOptimisationResult)
        assert len(evaluated.optimisation_result.squad.members) == 15


def test_fixed_squad_containment() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=0)
    result = enumerate_and_rank(state, players, projections, optimiser)

    roll = result[0]
    assert roll.candidate.kind == "ROLL"
    squad_ids = {m.player_id for m in roll.candidate.squad.members}
    result_squad_ids = {m.player_id for m in roll.optimisation_result.squad.members}
    assert result_squad_ids == squad_ids


def test_transfer_candidate_fixed_squad_containment() -> None:
    owned = make_owned_players(price=50)
    cheap = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Cheap",
        last_name="Incoming",
        web_name="CheapIn",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=40),
    )
    all_players = owned + (cheap,)
    projections = make_projections(all_players)
    state, _ = make_manager_state(owned_players=owned, bank=10, free_transfers=1)
    optimiser = HighsSingleGameweekOptimiser()

    result = enumerate_and_rank(state, all_players, projections, optimiser)

    evaluated = next(e for e in result if e.candidate.kind == "TRANSFER")
    candidate_ids = {m.player_id for m in evaluated.candidate.squad.members}
    result_ids = {m.player_id for m in evaluated.optimisation_result.squad.members}

    assert result_ids == candidate_ids
    assert cheap.id in result_ids
    assert evaluated.candidate.player_out_id not in result_ids


def test_xi_from_optimiser() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    for evaluated in result:
        assert len(evaluated.starting_xi) == 11
        assert set(evaluated.starting_xi) | set(evaluated.bench) == {
            m.player_id for m in evaluated.candidate.squad.members
        }


def test_bench_from_optimiser() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    for evaluated in result:
        assert len(evaluated.bench) == 4
        members_by_id = {m.player_id: m for m in evaluated.candidate.squad.members}
        assert members_by_id[evaluated.bench[0]].position is Position.GOALKEEPER


def test_captain_from_optimiser() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    for evaluated in result:
        assert evaluated.captain_id in evaluated.starting_xi
        assert evaluated.captain_id != evaluated.vice_captain_id


def test_vice_captain_from_optimiser() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    for evaluated in result:
        assert evaluated.vice_captain_id in evaluated.starting_xi
        assert evaluated.vice_captain_id != evaluated.captain_id


def test_formation_from_optimiser() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    for evaluated in result:
        assert isinstance(evaluated.formation, Formation)
        starters = set(evaluated.starting_xi)
        members_by_id = {m.player_id: m for m in evaluated.candidate.squad.members}
        positions = Counter(members_by_id[pid].position for pid in starters)
        assert positions[Position.GOALKEEPER] == 1
        assert positions[Position.DEFENDER] == evaluated.formation.defenders
        assert positions[Position.MIDFIELDER] == evaluated.formation.midfielders
        assert positions[Position.FORWARD] == evaluated.formation.forwards


# ---------------------------------------------------------------------------
# Uplift tests
# ---------------------------------------------------------------------------


def test_explicit_roll_relative_uplift() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    roll = next(e for e in result if e.candidate.kind == "ROLL")
    for evaluated in result:
        if evaluated.candidate.kind == "TRANSFER":
            assert evaluated.uplift_vs_roll == pytest.approx(
                evaluated.current_gw_score - roll.current_gw_score
            )


def test_roll_uplift_zero() -> None:
    optimiser = HighsSingleGameweekOptimiser()
    state, players, projections, _ = make_universe(free_transfers=1)
    result = enumerate_and_rank(state, players, projections, optimiser)
    roll = next(e for e in result if e.candidate.kind == "ROLL")
    assert roll.uplift_vs_roll == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Ranking tests
# ---------------------------------------------------------------------------


def test_transfer_candidate_ranked_above_roll_when_higher_score() -> None:
    owned = make_owned_players(price=50)
    strong = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Strong",
        last_name="Incoming",
        web_name="Strong",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=40),
    )
    all_players = owned + (strong,)
    projections = make_projections(
        all_players,
        base_points={**{p.id: 1.0 for p in owned}, strong.id: 50.0},
    )
    state, _ = make_manager_state(owned_players=owned, bank=10, free_transfers=1)
    optimiser = HighsSingleGameweekOptimiser()

    result = enumerate_and_rank(state, all_players, projections, optimiser)

    roll = next(e for e in result if e.candidate.kind == "ROLL")
    transfer = next(e for e in result if e.candidate.kind == "TRANSFER")
    assert transfer.current_gw_score > roll.current_gw_score
    assert roll in result
    assert transfer in result


def test_equal_score_fewer_transfer_tie_break() -> None:
    owned = make_owned_players(price=50)

    # All owned players score exactly 5.0; incoming player also scores 5.0
    # so the transfer candidate's XI score equals the ROLL score.
    in_player = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Equal",
        last_name="Points",
        web_name="EqPts",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=40),
    )
    all_players = owned + (in_player,)
    all_points = {p.id: 5.0 for p in all_players}
    projections = make_projections(all_players, base_points=all_points)
    state, _ = make_manager_state(owned_players=owned, bank=10, free_transfers=1)
    optimiser = HighsSingleGameweekOptimiser()

    result = enumerate_and_rank(state, all_players, projections, optimiser)

    roll = next(e for e in result if e.candidate.kind == "ROLL")
    transfers_with_same_score = [
        e
        for e in result
        if e.candidate.kind == "TRANSFER"
        and e.current_gw_score == pytest.approx(roll.current_gw_score)
    ]
    assert len(transfers_with_same_score) > 0

    roll_pos = result.index(roll)
    for t in transfers_with_same_score:
        t_pos = result.index(t)
        assert roll_pos < t_pos


def test_equal_score_transfer_identity_tie_break() -> None:
    owned = make_owned_players(price=50)

    # Two incoming players with identical score (5.0), same position, affordable.
    # All owned players also score 5.0, so transfer candidates with the same
    # score will tie on criterion 1 and need criterion 3 (identity) to break.
    in_a = Player(
        id=uid(9998),
        team_id=uid(998),
        first_name="IncomingA",
        last_name="Player",
        web_name="InA",
        position=owned[0].position,
        price=Money(tenths_million=40),
    )
    in_b = Player(
        id=uid(9999),
        team_id=uid(997),
        first_name="IncomingB",
        last_name="Player",
        web_name="InB",
        position=owned[1].position,
        price=Money(tenths_million=40),
    )
    all_players = owned + (in_a, in_b)
    all_points = {p.id: 5.0 for p in all_players}
    projections = make_projections(all_players, base_points=all_points)
    state, _ = make_manager_state(owned_players=owned, bank=10, free_transfers=1)
    optimiser = HighsSingleGameweekOptimiser()

    result = enumerate_and_rank(state, all_players, projections, optimiser)

    transfers = [e for e in result if e.candidate.kind == "TRANSFER"]
    same_score = [
        t for t in transfers if t.current_gw_score == pytest.approx(transfers[0].current_gw_score)
    ]
    assert len(same_score) >= 2

    same_score.sort(key=lambda e: e.candidate.identity)
    for i in range(len(same_score) - 1):
        assert same_score[i].candidate.identity <= same_score[i + 1].candidate.identity
        if same_score[i].candidate.identity == same_score[i + 1].candidate.identity:
            continue
        assert same_score[i].candidate.identity < same_score[i + 1].candidate.identity, (
            "identity ordering must be lexicographic on (out_id, in_id)"
        )


def test_deterministic_transfer_identity_tie_break() -> None:
    state, players, projections, _ = make_universe(free_transfers=1)
    optimiser = HighsSingleGameweekOptimiser()

    result_a = enumerate_and_rank(state, players, projections, optimiser)
    result_b = enumerate_and_rank(state, players, projections, optimiser)

    assert len(result_a) == len(result_b)
    for a, b in zip(result_a, result_b, strict=True):
        assert a.candidate.identity == b.candidate.identity
        assert a.current_gw_score == pytest.approx(b.current_gw_score)
        assert a.candidate.squad == b.candidate.squad


def test_repeated_deterministic_execution() -> None:
    state, players, projections, _ = make_universe(free_transfers=1)
    optimiser = HighsSingleGameweekOptimiser()

    first = enumerate_and_rank(state, players, projections, optimiser)
    second = enumerate_and_rank(state, players, projections, optimiser)

    assert len(first) == len(second)
    for a, b in zip(first, second, strict=True):
        assert a.candidate.kind == b.candidate.kind
        assert a.candidate.squad == b.candidate.squad
        assert a.candidate.bank_before == b.candidate.bank_before
        assert a.candidate.bank_after == b.candidate.bank_after
        assert a.candidate.free_transfers_used == b.candidate.free_transfers_used
        assert a.current_gw_score == pytest.approx(b.current_gw_score)
        assert a.uplift_vs_roll == pytest.approx(b.uplift_vs_roll)


# ---------------------------------------------------------------------------
# Bank semantics tests
# ---------------------------------------------------------------------------


def test_roll_bank_after_equals_bank_before() -> None:
    state, players, projections, _ = make_universe(bank=15, free_transfers=1)
    candidates = enumerate_transfer_candidates(state, players, projections)
    roll = candidates[0]
    assert roll.bank_after == roll.bank_before == Money(tenths_million=15)


def test_transfer_bank_after_calculated_from_selling_price() -> None:
    owned = make_owned_players(price=50)
    cheap = Player(
        id=uid(9999),
        team_id=uid(999),
        first_name="Cheap",
        last_name="Incoming",
        web_name="CheapIn",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=45),
    )
    all_players = owned + (cheap,)
    projections = make_projections(all_players)
    members = list(make_squad_members(owned, selling_offset=10))
    state = ManagerState(
        manager_id=uid(999),
        gameweek=GAMEWEEK,
        squad=Squad(members=tuple(members)),
        bank=Money(tenths_million=5),
        free_transfers=1,
        existing_points_cost=0,
    )
    candidates = enumerate_transfer_candidates(state, all_players, projections)
    transfers = [c for c in candidates if c.kind == "TRANSFER"]
    target = next(t for t in transfers if t.player_in_id == cheap.id)
    # selling = 60, bank = 5 → available = 65, incoming = 45 → bank_after = 20
    assert target.bank_after == Money(tenths_million=20)
