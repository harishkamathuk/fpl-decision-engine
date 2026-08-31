from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from itertools import combinations, product
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    GameweekNumber,
    Money,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
)
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser
from fpl_decision_engine.ports import (
    OptimisationEngine,
    OptimisationError,
    OptimisationErrorCode,
)

GAMEWEEK = GameweekNumber(value=1)
GENERATED_AT = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
QUOTAS = {
    Position.GOALKEEPER: 2,
    Position.DEFENDER: 5,
    Position.MIDFIELDER: 5,
    Position.FORWARD: 3,
}


def make_candidates(
    counts: dict[Position, int] | None = None,
    *,
    points_by_position: dict[Position, list[float]] | None = None,
    price: int | None = None,
) -> tuple[tuple[Player, ...], tuple[Projection, ...]]:
    counts = counts or {
        Position.GOALKEEPER: 3,
        Position.DEFENDER: 7,
        Position.MIDFIELDER: 7,
        Position.FORWARD: 5,
    }
    players: list[Player] = []
    projections: list[Projection] = []
    player_number = 1
    for position in Position:
        position_points = (points_by_position or {}).get(position)
        for position_index in range(counts[position]):
            player_id = UUID(int=player_number)
            team_id = UUID(int=10_000 + ((player_number - 1) % 8))
            player_price = (
                price
                if price is not None
                else {
                    Position.GOALKEEPER: 40,
                    Position.DEFENDER: 45,
                    Position.MIDFIELDER: 55,
                    Position.FORWARD: 60,
                }[position]
                + position_index
            )
            expected_points = (
                position_points[position_index]
                if position_points is not None
                else 20.0 - (player_number * 0.4)
            )
            players.append(
                Player(
                    id=player_id,
                    team_id=team_id,
                    first_name=f"First{player_number}",
                    last_name=f"Last{player_number}",
                    web_name=f"P{player_number}",
                    position=position,
                    price=Money(tenths_million=player_price),
                    active=player_number != 1,
                )
            )
            projections.append(
                Projection(
                    player_id=player_id,
                    gameweek=GAMEWEEK,
                    expected_points=expected_points,
                    appearance_probability=0.01,
                    source="synthetic",
                    model_version="test-v1",
                    generated_at=GENERATED_AT,
                )
            )
            player_number += 1
    return tuple(players), tuple(projections)


def request_for(
    players: tuple[Player, ...],
    projections: tuple[Projection, ...],
    **updates: object,
) -> SingleGameweekOptimisationRequest:
    values: dict[str, object] = {
        "target_gameweek": GAMEWEEK,
        "players": players,
        "projections": projections,
    }
    values.update(updates)
    return SingleGameweekOptimisationRequest.model_validate(values)


def update_projections(
    projections: tuple[Projection, ...],
    updates: dict[UUID, dict[str, object]],
) -> tuple[Projection, ...]:
    return tuple(
        projection.model_copy(update=updates[projection.player_id])
        if projection.player_id in updates
        else projection
        for projection in projections
    )


def result_without_runtime_or_diagnostics(result: object) -> object:
    assert hasattr(result, "model_copy")
    return result.model_copy(update={"runtime_seconds": 0, "diagnostics": ()})


@pytest.fixture(scope="module")
def baseline() -> tuple[tuple[Player, ...], tuple[Projection, ...], object]:
    players, projections = make_candidates()
    result = HighsSingleGameweekOptimiser().optimise(request_for(players, projections))
    return players, projections, result


def test_valid_optimisation_returns_complete_legal_recommendation(
    baseline: tuple[tuple[Player, ...], tuple[Projection, ...], object],
) -> None:
    players, projections, untyped_result = baseline
    result = untyped_result
    assert hasattr(result, "squad")
    player_by_id = {player.id: player for player in players}
    points = {projection.player_id: projection.expected_points for projection in projections}
    appearances = {
        projection.player_id: projection.appearance_probability for projection in projections
    }
    squad_ids = {member.player_id for member in result.squad.members}
    starter_ids = set(result.starting_xi)

    assert len(squad_ids) == 15
    assert Counter(member.position for member in result.squad.members) == Counter(QUOTAS)
    assert len(starter_ids) == 11
    assert result.formation.defenders in range(3, 6)
    assert result.formation.midfielders in range(2, 6)
    assert result.formation.forwards in range(1, 4)
    assert (
        sum(player_by_id[player_id].position is Position.GOALKEEPER for player_id in starter_ids)
        == 1
    )
    assert players[0].id in squad_ids  # Player.active is not an availability policy.
    assert result.squad_cost.tenths_million <= 1000
    assert result.bank_remaining.tenths_million == 1000 - result.squad_cost.tenths_million
    assert max(Counter(member.team_id for member in result.squad.members).values()) <= 3
    assert result.captain_id in starter_ids
    assert result.vice_captain_id in starter_ids
    assert result.captain_id != result.vice_captain_id
    assert result.squad.members[0].purchase_price is not None

    assert player_by_id[result.bench[0]].position is Position.GOALKEEPER
    assert all(
        player_by_id[player_id].position is not Position.GOALKEEPER
        for player_id in result.bench[1:]
    )
    assert list(result.bench[1:]) == sorted(
        result.bench[1:], key=lambda player_id: (-points[player_id], str(player_id))
    )
    expected_primary = sum(points[player_id] for player_id in result.starting_xi)
    expected_primary += points[result.captain_id]
    captain_appearance = appearances[result.captain_id]
    assert captain_appearance is not None
    expected_primary += (1 - captain_appearance) * points[result.vice_captain_id]
    assert result.primary_objective == pytest.approx(expected_primary)
    assert result.primary_objective > expected_primary * 0.01
    assert result.solver_name.startswith("HiGHS ")
    assert result.solver_status == "Optimal"
    assert result.objective_bound == pytest.approx(result.primary_objective)
    assert result.mip_gap == pytest.approx(0)
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "primary_solve",
        "secondary_solve",
        "minimum_cost",
        "captain_fallback_evaluated",
    }


def test_optimiser_satisfies_generic_port() -> None:
    assert isinstance(HighsSingleGameweekOptimiser(), OptimisationEngine)


def test_include_exclude_and_forced_scenarios_are_applied() -> None:
    players, projections = make_candidates()
    low_forward = next(
        player for player in reversed(players) if player.position is Position.FORWARD
    )
    high_midfielder = next(player for player in players if player.position is Position.MIDFIELDER)
    low_defender = next(
        player for player in reversed(players) if player.position is Position.DEFENDER
    )
    forced_captain = next(player for player in players if player.position is Position.FORWARD)
    forced_vice = next(player for player in players if player.position is Position.GOALKEEPER)
    request = request_for(
        players,
        projections,
        must_include_in_squad=frozenset({low_forward.id}),
        excluded_players=frozenset({high_midfielder.id}),
        forced_starters=frozenset({low_defender.id}),
        forced_captain=forced_captain.id,
        forced_vice_captain=forced_vice.id,
    )

    result = HighsSingleGameweekOptimiser().optimise(request)
    squad_ids = {member.player_id for member in result.squad.members}

    assert low_forward.id in squad_ids
    assert high_midfielder.id not in squad_ids
    assert low_defender.id in result.starting_xi
    assert result.captain_id == forced_captain.id
    assert result.vice_captain_id == forced_vice.id


def test_forced_vice_captain_cannot_be_selected_as_captain() -> None:
    players, projections = make_candidates()
    highest_projected = players[0]

    result = HighsSingleGameweekOptimiser().optimise(
        request_for(players, projections, forced_vice_captain=highest_projected.id)
    )

    assert result.vice_captain_id == highest_projected.id
    assert result.captain_id != highest_projected.id


@pytest.mark.parametrize(
    "update_factory,match",
    [
        (
            lambda player_id: {
                "must_include_in_squad": frozenset({player_id}),
                "excluded_players": frozenset({player_id}),
            },
            "both included and excluded",
        ),
        (
            lambda player_id: {
                "forced_captain": player_id,
                "forced_vice_captain": player_id,
            },
            "captain and vice-captain must differ",
        ),
        (
            lambda player_id: {
                "forced_starters": frozenset(UUID(int=index) for index in range(1, 13))
            },
            "too many forced starters",
        ),
    ],
)
def test_contradictory_scenarios_fail_before_primary_solve(
    update_factory: object, match: str
) -> None:
    players, projections = make_candidates()
    factory = update_factory
    assert callable(factory)
    updates = factory(players[0].id)

    with pytest.raises(OptimisationError, match=match) as error:
        HighsSingleGameweekOptimiser().optimise(request_for(players, projections, **updates))

    assert error.value.code is OptimisationErrorCode.INVALID_INPUT


def test_insufficient_position_candidates_are_rejected() -> None:
    players, projections = make_candidates()
    retained_players = tuple(
        player
        for player in players
        if player.position is not Position.GOALKEEPER or player.id == players[0].id
    )
    retained_ids = {player.id for player in retained_players}
    retained_projections = tuple(
        projection for projection in projections if projection.player_id in retained_ids
    )

    with pytest.raises(OptimisationError, match="insufficient GK candidates") as error:
        HighsSingleGameweekOptimiser().optimise(request_for(retained_players, retained_projections))

    assert error.value.diagnostics[0].code == "insufficient_position"


def test_budget_below_exact_minimum_has_structured_diagnostics() -> None:
    players, projections = make_candidates(price=50)

    with pytest.raises(OptimisationError, match="minimum legal squad cost") as error:
        HighsSingleGameweekOptimiser().optimise(
            request_for(players, projections, budget=Money(tenths_million=749))
        )

    assert error.value.code is OptimisationErrorCode.INFEASIBLE
    assert dict(error.value.diagnostics[0].context) == {
        "budget_tenths": "749",
        "minimum_cost_tenths": "750",
    }


def test_impossible_club_distribution_preserves_solver_status() -> None:
    players, projections = make_candidates()
    four_clubs = tuple(
        player.model_copy(update={"team_id": UUID(int=20_000 + (index % 4))})
        for index, player in enumerate(players)
    )

    with pytest.raises(OptimisationError, match="no legal squad") as error:
        HighsSingleGameweekOptimiser().optimise(request_for(four_clubs, projections))

    assert error.value.code is OptimisationErrorCode.INFEASIBLE
    assert error.value.solver_status == "Infeasible"
    assert error.value.diagnostics[0].code == "minimum_cost_status"


@pytest.mark.parametrize(
    "case", ["duplicate_player", "duplicate_projection", "missing", "wrong_gw"]
)
def test_malformed_candidate_projection_sets_are_rejected(case: str) -> None:
    players, projections = make_candidates()
    if case == "duplicate_player":
        request = request_for(players + (players[0],), projections)
        match = "duplicate candidate player"
    elif case == "duplicate_projection":
        request = request_for(players, projections + (projections[0],))
        match = "duplicate projection"
    elif case == "missing":
        request = request_for(players, projections[1:])
        match = "exactly one target-gameweek projection"
    else:
        wrong = projections[0].model_copy(update={"gameweek": GameweekNumber(value=2)})
        request = request_for(players, (wrong,) + projections[1:])
        match = "targets gameweek 2"

    with pytest.raises(OptimisationError, match=match) as error:
        HighsSingleGameweekOptimiser().optimise(request)

    assert error.value.code is OptimisationErrorCode.INVALID_INPUT


def test_secondary_solve_improves_bench_without_reducing_primary() -> None:
    points = {
        Position.GOALKEEPER: [10, 1],
        Position.DEFENDER: [9, 8, 7, 0.9, 0.8, 0.1],
        Position.MIDFIELDER: [20, 19, 18, 17, 16],
        Position.FORWARD: [15, 14, 1],
    }
    counts = {position: len(values) for position, values in points.items()}
    players, projections = make_candidates(counts, points_by_position=points, price=50)
    point_by_id = {projection.player_id: projection.expected_points for projection in projections}
    weakest_defender = min(
        (player for player in players if player.position is Position.DEFENDER),
        key=lambda player: point_by_id[player.id],
    )

    result = HighsSingleGameweekOptimiser().optimise(request_for(players, projections))
    squad_ids = {member.player_id for member in result.squad.members}
    realised_primary = sum(point_by_id[player_id] for player_id in result.starting_xi)
    realised_primary += point_by_id[result.captain_id]
    realised_primary += 0.99 * point_by_id[result.vice_captain_id]

    assert weakest_defender.id not in squad_ids
    assert result.primary_objective == pytest.approx(realised_primary)
    assert result.secondary_squad_objective == pytest.approx(
        sum(point_by_id[player_id] for player_id in squad_ids)
    )


def test_output_is_independent_of_candidate_input_order() -> None:
    players, projections = make_candidates()
    optimiser = HighsSingleGameweekOptimiser()

    forward = optimiser.optimise(request_for(players, projections))
    reversed_result = optimiser.optimise(
        request_for(tuple(reversed(players)), tuple(reversed(projections)))
    )

    assert forward.squad == reversed_result.squad
    assert forward.starting_xi == reversed_result.starting_xi
    assert forward.captain_id == reversed_result.captain_id
    assert forward.vice_captain_id == reversed_result.vice_captain_id
    assert forward.bench == reversed_result.bench
    assert forward.primary_objective == pytest.approx(reversed_result.primary_objective)


def brute_force_primary(
    players: tuple[Player, ...], projections: tuple[Projection, ...], budget: int
) -> float:
    """Enumerate every legal squad, XI, captain and vice independently of the MILP."""

    by_position = {
        position: tuple(player for player in players if player.position is position)
        for position in Position
    }
    points = {projection.player_id: projection.expected_points for projection in projections}
    appearances = {
        projection.player_id: projection.appearance_probability for projection in projections
    }
    assert all(probability is not None for probability in appearances.values())
    best = float("-inf")
    position_squads = [
        tuple(combinations(by_position[position], quota)) for position, quota in QUOTAS.items()
    ]
    for grouped_squad in product(*position_squads):
        squad = tuple(player for group in grouped_squad for player in group)
        if sum(player.price.tenths_million for player in squad) > budget:
            continue
        if max(Counter(player.team_id for player in squad).values()) > 3:
            continue
        squad_by_position = {
            position: tuple(player for player in squad if player.position is position)
            for position in Position
        }
        for defenders in range(3, 6):
            for midfielders in range(2, 6):
                forwards = 10 - defenders - midfielders
                if not 1 <= forwards <= 3:
                    continue
                lineup_groups = (
                    combinations(squad_by_position[Position.GOALKEEPER], 1),
                    combinations(squad_by_position[Position.DEFENDER], defenders),
                    combinations(squad_by_position[Position.MIDFIELDER], midfielders),
                    combinations(squad_by_position[Position.FORWARD], forwards),
                )
                for grouped_lineup in product(*lineup_groups):
                    lineup = tuple(player for group in grouped_lineup for player in group)
                    lineup_points = sum(points[player.id] for player in lineup)
                    for captain in lineup:
                        captain_appearance = appearances[captain.id]
                        assert captain_appearance is not None
                        vice_points = max(
                            points[player.id] for player in lineup if player.id != captain.id
                        )
                        best = max(
                            best,
                            lineup_points
                            + points[captain.id]
                            + (1 - captain_appearance) * vice_points,
                        )
    return best


def test_highs_primary_objective_matches_independent_enumeration_oracle() -> None:
    counts = {
        Position.GOALKEEPER: 2,
        Position.DEFENDER: 5,
        Position.MIDFIELDER: 6,
        Position.FORWARD: 3,
    }
    players, projections = make_candidates(counts, price=50)
    oracle = brute_force_primary(players, projections, budget=1000)

    result = HighsSingleGameweekOptimiser().optimise(request_for(players, projections))

    assert result.primary_objective == pytest.approx(oracle)


def test_fallback_changes_captain_choice_and_matches_exact_formula() -> None:
    points = {
        Position.GOALKEEPER: [1, 0],
        Position.DEFENDER: [1, 1, 1, 0, 0],
        Position.MIDFIELDER: [10, 9, 1, 1, 1],
        Position.FORWARD: [1, 1, 0],
    }
    counts = {position: len(values) for position, values in points.items()}
    players, projections = make_candidates(counts, points_by_position=points, price=50)
    midfielders = [player for player in players if player.position is Position.MIDFIELDER]
    highest, fallback_captain = midfielders[:2]
    projections = update_projections(
        projections,
        {
            projection.player_id: {"appearance_probability": 1.0}
            for projection in projections
        }
        | {fallback_captain.id: {"appearance_probability": 0.0}},
    )
    optimiser = HighsSingleGameweekOptimiser()

    mean_only = optimiser.optimise(
        request_for(players, projections, captain_fallback=False)
    )
    result = optimiser.optimise(request_for(players, projections))
    point_by_id = {projection.player_id: projection.expected_points for projection in projections}

    assert mean_only.captain_id == highest.id
    assert result.captain_id == fallback_captain.id
    assert result.vice_captain_id == highest.id
    assert result.captain_id in result.starting_xi
    assert result.vice_captain_id in result.starting_xi
    assert result.captain_id != result.vice_captain_id
    assert result.primary_objective == pytest.approx(
        sum(point_by_id[player_id] for player_id in result.starting_xi)
        + point_by_id[result.captain_id]
        + point_by_id[result.vice_captain_id]
    )
    assert "captain_fallback_evaluated" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_forced_vice_contributes_to_fallback_objective() -> None:
    players, projections = make_candidates(price=50)
    captain = players[0]
    vice = players[-1]
    projections = update_projections(
        projections,
        {captain.id: {"appearance_probability": 0.25}},
    )
    points = {projection.player_id: projection.expected_points for projection in projections}

    result = HighsSingleGameweekOptimiser().optimise(
        request_for(
            players,
            projections,
            forced_captain=captain.id,
            forced_vice_captain=vice.id,
        )
    )

    assert result.vice_captain_id == vice.id
    assert result.primary_objective == pytest.approx(
        sum(points[player_id] for player_id in result.starting_xi)
        + points[captain.id]
        + 0.75 * points[vice.id]
    )


def test_high_appearance_probability_is_near_mean_only() -> None:
    players, projections = make_candidates(price=50)
    captain = players[0]
    projections = update_projections(
        projections,
        {captain.id: {"appearance_probability": 0.999}},
    )
    optimiser = HighsSingleGameweekOptimiser()
    updates = {"forced_captain": captain.id}

    mean_only = optimiser.optimise(
        request_for(players, projections, captain_fallback=False, **updates)
    )
    result = optimiser.optimise(request_for(players, projections, **updates))
    points = {projection.player_id: projection.expected_points for projection in projections}

    assert result.primary_objective - mean_only.primary_objective == pytest.approx(
        0.001 * points[result.vice_captain_id]
    )


def test_missing_selected_captain_reruns_exact_mean_only_semantics() -> None:
    players, projections = make_candidates(price=50)
    captain = players[0]
    projections = update_projections(
        projections,
        {
            projection.player_id: {"appearance_probability": 1.0}
            for projection in projections
        }
        | {captain.id: {"appearance_probability": None}},
    )
    optimiser = HighsSingleGameweekOptimiser()
    request = request_for(players, projections, forced_captain=captain.id)

    result = optimiser.optimise(request)
    mean_only = optimiser.optimise(request.model_copy(update={"captain_fallback": False}))

    assert result_without_runtime_or_diagnostics(result) == result_without_runtime_or_diagnostics(
        mean_only
    )
    assert "captain_fallback_unavailable" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_all_missing_probabilities_use_mean_only_semantics() -> None:
    players, projections = make_candidates(price=50)
    projections = update_projections(
        projections,
        {
            projection.player_id: {"appearance_probability": None}
            for projection in projections
        },
    )
    optimiser = HighsSingleGameweekOptimiser()
    request = request_for(players, projections)

    result = optimiser.optimise(request)
    mean_only = optimiser.optimise(request.model_copy(update={"captain_fallback": False}))

    assert result_without_runtime_or_diagnostics(result) == result_without_runtime_or_diagnostics(
        mean_only
    )
    assert "captain_fallback_unavailable" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_mixed_probabilities_with_known_captain_use_fallback() -> None:
    players, projections = make_candidates(price=50)
    captain = players[0]
    missing = players[-1]
    projections = update_projections(
        projections,
        {
            captain.id: {"appearance_probability": 0.5},
            missing.id: {"appearance_probability": None},
        },
    )
    points = {projection.player_id: projection.expected_points for projection in projections}

    result = HighsSingleGameweekOptimiser().optimise(
        request_for(players, projections, forced_captain=captain.id)
    )

    assert result.primary_objective == pytest.approx(
        sum(points[player_id] for player_id in result.starting_xi)
        + points[captain.id]
        + 0.5 * points[result.vice_captain_id]
    )
    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert "captain_fallback_evaluated" in codes
    assert "captain_fallback_unavailable" not in codes


def test_probability_one_is_known_and_has_zero_fallback_value() -> None:
    players, projections = make_candidates(price=50)
    captain = players[0]
    projections = update_projections(
        projections,
        {captain.id: {"appearance_probability": 1.0}},
    )
    optimiser = HighsSingleGameweekOptimiser()
    request = request_for(players, projections, forced_captain=captain.id)

    result = optimiser.optimise(request)
    mean_only = optimiser.optimise(request.model_copy(update={"captain_fallback": False}))

    assert result.primary_objective == pytest.approx(mean_only.primary_objective)
    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert "captain_fallback_evaluated" in codes
    assert "captain_fallback_unavailable" not in codes


def test_negative_forced_vice_points_remain_in_mandatory_fallback_value() -> None:
    players, projections = make_candidates(price=50)
    captain = players[0]
    vice = players[-1]
    projections = update_projections(
        projections,
        {
            captain.id: {"appearance_probability": 0.0},
            vice.id: {"expected_points": -2.0},
        },
    )
    optimiser = HighsSingleGameweekOptimiser()
    updates = {"forced_captain": captain.id, "forced_vice_captain": vice.id}

    result = optimiser.optimise(request_for(players, projections, **updates))
    mean_only = optimiser.optimise(
        request_for(players, projections, captain_fallback=False, **updates)
    )

    assert result.vice_captain_id == vice.id
    assert result.primary_objective - mean_only.primary_objective == pytest.approx(-2.0)
