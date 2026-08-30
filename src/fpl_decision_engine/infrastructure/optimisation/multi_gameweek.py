"""Direct HiGHS joint multi-gameweek transfer planning."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import isfinite
from time import perf_counter
from uuid import UUID

from highspy import (
    Highs,
    HighsInfo,
    HighsModelStatus,
    HighsStatus,
    HighsVarType,
    highs_linear_expression,
    highs_var,
)

from fpl_decision_engine.domain import (
    Formation,
    Money,
    MultiGameweekPlanningRequest,
    MultiGameweekPlanningResult,
    OptimisationDiagnostic,
    PlannedGameweek,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
    Squad,
    SquadMember,
    TransferPair,
)
from fpl_decision_engine.ports import OptimisationError, OptimisationErrorCode

from .highs import HighsSingleGameweekOptimiser

_SQUAD_QUOTAS = {
    Position.GOALKEEPER: 2,
    Position.DEFENDER: 5,
    Position.MIDFIELDER: 5,
    Position.FORWARD: 3,
}
_STARTER_BOUNDS = {
    Position.GOALKEEPER: (1, 1),
    Position.DEFENDER: (3, 5),
    Position.MIDFIELDER: (2, 5),
    Position.FORWARD: (1, 3),
}
_POSITION_ORDER = {
    Position.GOALKEEPER: 0,
    Position.DEFENDER: 1,
    Position.MIDFIELDER: 2,
    Position.FORWARD: 3,
}
_PRIMARY_TOLERANCE = 1e-7


@dataclass(frozen=True)
class _Candidate:
    player: Player
    projections: tuple[Projection, ...]
    initial_member: SquadMember | None


@dataclass(frozen=True)
class _FtChoice:
    free_transfers: int
    transfer_count: int
    variable: highs_var


@dataclass(frozen=True)
class _WeekVariables:
    squad: tuple[highs_var, ...]
    starter: tuple[highs_var, ...]
    captain: tuple[highs_var, ...]
    transfer_in: tuple[highs_var, ...]
    transfer_out: tuple[highs_var, ...]
    legacy_owned: tuple[highs_var, ...]
    legacy_out: tuple[highs_var, ...]
    free_transfers: highs_var
    paid_transfers: highs_var
    next_free_transfers: highs_var
    bank_after: highs_var
    ft_choices: tuple[_FtChoice, ...]


def _expression(terms: Iterable[tuple[float | int, highs_var]]) -> highs_linear_expression:
    expression = highs_linear_expression()
    for coefficient, variable in terms:
        expression += coefficient * variable
    return expression


def _configured_model() -> Highs:
    model = Highs()
    for option, value in (
        ("output_flag", False),
        ("parallel", "off"),
        ("threads", 1),
        ("random_seed", 0),
        ("mip_rel_gap", 0.0),
        ("mip_abs_gap", 1e-9),
    ):
        if model.setOptionValue(option, value) != HighsStatus.kOk:
            raise RuntimeError(f"HiGHS rejected deterministic option {option!r}")
    return model


def _diagnostic(code: str, message: str, **context: object) -> OptimisationDiagnostic:
    return OptimisationDiagnostic(
        code=code,
        message=message,
        context=tuple(sorted((key, str(value)) for key, value in context.items())),
    )


def _fail_input(message: str, code: str, **context: object) -> None:
    raise OptimisationError(
        message,
        code=OptimisationErrorCode.INVALID_INPUT,
        diagnostics=(_diagnostic(code, message, **context),),
    )


def _finite_or_none(value: float) -> float | None:
    return value if isfinite(value) else None


class HighsMultiGameweekPlanner:
    """Jointly optimise a deterministic normal-transfer trajectory.

    Only the first Gameweek transfers are actionable. Later moves express conditional
    planning context under frozen prices and forecasts; callers should re-optimise as
    new information arrives rather than execute the future path blindly.
    """

    @property
    def engine_id(self) -> str:
        return "highs-multi-gameweek-planner-v1"

    def optimise(self, request: MultiGameweekPlanningRequest) -> MultiGameweekPlanningResult:
        try:
            candidates = self._prepare_candidates(request)
            hold = self._hold_trajectory(candidates, request)
            return self._solve(candidates, request, hold)
        except OptimisationError:
            raise
        except Exception as exc:
            raise OptimisationError(
                "HiGHS multi-gameweek planning failed unexpectedly",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                diagnostics=(
                    _diagnostic(
                        "solver_exception",
                        "unexpected multi-gameweek solver failure",
                        type=type(exc).__name__,
                    ),
                ),
            ) from exc

    def _prepare_candidates(self, request: MultiGameweekPlanningRequest) -> tuple[_Candidate, ...]:
        players: dict[UUID, Player] = {}
        for player in request.players:
            if player.id in players:
                _fail_input("duplicate candidate player", "duplicate_player", player_id=player.id)
            players[player.id] = player

        target_values = {gameweek.value for gameweek in request.target_gameweeks}
        projections: dict[tuple[UUID, int], Projection] = {}
        for projection in request.projections:
            if projection.player_id not in players:
                _fail_input(
                    "projection references an unknown candidate",
                    "unknown_projection_player",
                    player_id=projection.player_id,
                )
            if projection.gameweek.value not in target_values:
                _fail_input(
                    "projection falls outside the planning horizon",
                    "projection_outside_horizon",
                    gameweek=projection.gameweek.value,
                )
            key = (projection.player_id, projection.gameweek.value)
            if key in projections:
                _fail_input(
                    "duplicate player/gameweek projection",
                    "duplicate_projection",
                    player_id=projection.player_id,
                    gameweek=projection.gameweek.value,
                )
            projections[key] = projection

        missing = [
            f"{player_id}:{gameweek.value}"
            for player_id in players
            for gameweek in request.target_gameweeks
            if (player_id, gameweek.value) not in projections
        ]
        if missing:
            _fail_input(
                "every candidate requires one projection for every planned gameweek",
                "missing_projection",
                first_missing=missing[0],
                missing_count=len(missing),
            )

        initial_members = {
            member.player_id: member for member in request.initial_manager_state.squad.members
        }
        missing_owned = initial_members.keys() - players.keys()
        if missing_owned:
            _fail_input(
                "manager squad contains players absent from candidates",
                "missing_owned_player",
                player_ids=",".join(sorted(map(str, missing_owned))),
            )
        unknown_excluded = request.excluded_players - players.keys()
        if unknown_excluded:
            _fail_input(
                "excluded players must belong to the candidate universe",
                "unknown_excluded_player",
            )
        for player_id, member in initial_members.items():
            player = players[player_id]
            if member.team_id != player.team_id or member.position != player.position:
                _fail_input(
                    "manager squad identity metadata does not match canonical player",
                    "owned_player_metadata",
                    player_id=player_id,
                )
            if member.purchase_price is None or member.selling_price is None:
                _fail_input(
                    "manager squad member lacks transfer prices",
                    "missing_owned_prices",
                    player_id=player_id,
                )

        available_positions = Counter(
            player.position
            for player_id, player in players.items()
            if player_id not in request.excluded_players
        )
        for position, quota in _SQUAD_QUOTAS.items():
            if available_positions[position] < quota:
                _fail_input(
                    f"insufficient {position.value} candidates",
                    "insufficient_position",
                    position=position.value,
                )

        return tuple(
            _Candidate(
                player=players[player_id],
                projections=tuple(
                    projections[(player_id, gameweek.value)]
                    for gameweek in request.target_gameweeks
                ),
                initial_member=initial_members.get(player_id),
            )
            for player_id in sorted(players, key=str)
        )

    def _hold_trajectory(
        self,
        candidates: tuple[_Candidate, ...],
        request: MultiGameweekPlanningRequest,
    ) -> tuple[PlannedGameweek, ...]:
        owned = tuple(candidate for candidate in candidates if candidate.initial_member is not None)
        budget = Money(
            tenths_million=sum(candidate.player.price.tenths_million for candidate in owned)
        )
        free_transfers = request.initial_manager_state.free_transfers
        trajectory: list[PlannedGameweek] = []
        for offset, (gameweek, weight) in enumerate(
            zip(request.target_gameweeks, request.resolved_weights, strict=True)
        ):
            lineup = HighsSingleGameweekOptimiser().optimise(
                SingleGameweekOptimisationRequest(
                    target_gameweek=gameweek,
                    players=tuple(candidate.player for candidate in owned),
                    projections=tuple(candidate.projections[offset] for candidate in owned),
                    budget=budget,
                    captain_fallback=False,
                )
            )
            next_free_transfers = min(5, max(1, free_transfers + 1))
            squad_points = sum(candidate.projections[offset].expected_points for candidate in owned)
            trajectory.append(
                PlannedGameweek(
                    gameweek=gameweek,
                    squad=request.initial_manager_state.squad,
                    starting_xi=lineup.starting_xi,
                    captain_id=lineup.captain_id,
                    vice_captain_id=lineup.vice_captain_id,
                    bench=lineup.bench,
                    formation=lineup.formation,
                    transfers=(),
                    transfer_count=0,
                    free_transfers_available=free_transfers,
                    free_transfers_used=0,
                    paid_transfers=0,
                    hit_cost=0,
                    bank_before=request.initial_manager_state.bank,
                    bank_after=request.initial_manager_state.bank,
                    gross_expected_score=lineup.primary_objective,
                    net_expected_score=lineup.primary_objective,
                    discount_weight=weight,
                    weighted_contribution=weight * lineup.primary_objective,
                    next_free_transfers=next_free_transfers,
                    squad_expected_points=squad_points,
                )
            )
            free_transfers = next_free_transfers
        return tuple(trajectory)

    def _solve(
        self,
        candidates: tuple[_Candidate, ...],
        request: MultiGameweekPlanningRequest,
        hold: tuple[PlannedGameweek, ...],
    ) -> MultiGameweekPlanningResult:
        model = _configured_model()
        variables = self._create_variables(model, candidates, request)
        self._add_constraints(model, candidates, variables, request)

        primary = highs_linear_expression()
        total_transfers = highs_linear_expression()
        squad_quality = highs_linear_expression()
        for offset, week in enumerate(variables):
            weight = request.resolved_weights[offset]
            gross = _expression(
                (candidate.projections[offset].expected_points, week.starter[index])
                for index, candidate in enumerate(candidates)
            )
            gross += _expression(
                (candidate.projections[offset].expected_points, week.captain[index])
                for index, candidate in enumerate(candidates)
            )
            primary += weight * gross - (weight * 4) * week.paid_transfers
            total_transfers += _expression((1, variable) for variable in week.transfer_in)
            squad_quality += weight * _expression(
                (candidate.projections[offset].expected_points, week.squad[index])
                for index, candidate in enumerate(candidates)
            )

        started_at = perf_counter()
        model.maximize(primary)
        self._run_or_raise(model, "primary")
        primary_objective = model.getObjectiveValue()
        primary_info = model.getInfo()
        tolerance = _PRIMARY_TOLERANCE * max(1.0, abs(primary_objective))
        model.addConstr(primary >= primary_objective - tolerance, name="preserve_primary_lower")
        model.addConstr(primary <= primary_objective + tolerance, name="preserve_primary_upper")

        model.minimize(total_transfers)
        self._run_or_raise(model, "transfer_count")
        optimal_transfer_count = round(model.getObjectiveValue())
        model.addConstr(
            total_transfers == optimal_transfer_count,
            name="preserve_total_transfer_count",
        )

        model.maximize(squad_quality)
        self._run_or_raise(model, "squad_quality")
        runtime = perf_counter() - started_at
        return self._build_result(
            model,
            candidates,
            variables,
            request,
            hold,
            primary_objective,
            model.getObjectiveValue(),
            primary_info,
            tolerance,
            runtime,
        )

    def _create_variables(
        self,
        model: Highs,
        candidates: tuple[_Candidate, ...],
        request: MultiGameweekPlanningRequest,
    ) -> tuple[_WeekVariables, ...]:
        size = len(candidates)
        weeks: list[_WeekVariables] = []
        for offset, transfer_limit in enumerate(request.resolved_transfer_limits):

            def binaries(prefix: str, week_offset: int = offset) -> tuple[highs_var, ...]:
                return tuple(
                    model.addVariable(
                        lb=0,
                        ub=1,
                        type=HighsVarType.kInteger,
                        name=f"{prefix}_{week_offset}_{index}",
                    )
                    for index in range(size)
                )

            choices = tuple(
                _FtChoice(
                    free_transfers=free_transfers,
                    transfer_count=transfer_count,
                    variable=model.addVariable(
                        lb=0,
                        ub=1,
                        type=HighsVarType.kInteger,
                        name=f"ft_choice_{offset}_{free_transfers}_{transfer_count}",
                    ),
                )
                for free_transfers in range(6)
                for transfer_count in range(transfer_limit + 1)
            )
            weeks.append(
                _WeekVariables(
                    squad=binaries("squad"),
                    starter=binaries("starter"),
                    captain=binaries("captain"),
                    transfer_in=binaries("in"),
                    transfer_out=binaries("out"),
                    legacy_owned=binaries("legacy"),
                    legacy_out=binaries("legacy_out"),
                    free_transfers=model.addVariable(
                        lb=0,
                        ub=5,
                        type=HighsVarType.kInteger,
                        name=f"free_transfers_{offset}",
                    ),
                    paid_transfers=model.addVariable(
                        lb=0,
                        ub=transfer_limit,
                        type=HighsVarType.kInteger,
                        name=f"paid_transfers_{offset}",
                    ),
                    next_free_transfers=model.addVariable(
                        lb=1,
                        ub=5,
                        type=HighsVarType.kInteger,
                        name=f"next_free_transfers_{offset}",
                    ),
                    bank_after=model.addVariable(
                        lb=0,
                        ub=model.getInfinity(),
                        type=HighsVarType.kInteger,
                        name=f"bank_after_{offset}",
                    ),
                    ft_choices=choices,
                )
            )
        return tuple(weeks)

    def _add_constraints(
        self,
        model: Highs,
        candidates: tuple[_Candidate, ...],
        variables: tuple[_WeekVariables, ...],
        request: MultiGameweekPlanningRequest,
    ) -> None:
        for offset, week in enumerate(variables):
            transfer_count = _expression((1, variable) for variable in week.transfer_in)
            model.addConstr(
                transfer_count <= request.resolved_transfer_limits[offset],
                name=f"maximum_transfers_{offset}",
            )
            model.addConstr(
                transfer_count == _expression((1, variable) for variable in week.transfer_out),
                name=f"balanced_transfers_{offset}",
            )
            model.addConstr(
                _expression((1, choice.variable) for choice in week.ft_choices) == 1,
                name=f"one_ft_state_{offset}",
            )
            model.addConstr(
                week.free_transfers
                == _expression(
                    (choice.free_transfers, choice.variable) for choice in week.ft_choices
                ),
                name=f"ft_state_value_{offset}",
            )
            model.addConstr(
                transfer_count
                == _expression(
                    (choice.transfer_count, choice.variable) for choice in week.ft_choices
                ),
                name=f"ft_state_transfer_count_{offset}",
            )
            model.addConstr(
                week.paid_transfers
                == _expression(
                    (
                        max(0, choice.transfer_count - choice.free_transfers),
                        choice.variable,
                    )
                    for choice in week.ft_choices
                ),
                name=f"paid_transfer_state_{offset}",
            )
            model.addConstr(
                week.next_free_transfers
                == _expression(
                    (
                        min(
                            5,
                            max(
                                1,
                                choice.free_transfers - choice.transfer_count + 1,
                            ),
                        ),
                        choice.variable,
                    )
                    for choice in week.ft_choices
                ),
                name=f"next_ft_state_{offset}",
            )
            if offset == 0:
                model.addConstr(
                    week.free_transfers == request.initial_manager_state.free_transfers,
                    name="initial_free_transfers",
                )
            else:
                model.addConstr(
                    week.free_transfers == variables[offset - 1].next_free_transfers,
                    name=f"carry_free_transfers_{offset}",
                )

            model.addConstr(
                _expression((1, variable) for variable in week.squad) == 15,
                name=f"squad_size_{offset}",
            )
            model.addConstr(
                _expression((1, variable) for variable in week.starter) == 11,
                name=f"starting_xi_{offset}",
            )
            model.addConstr(
                _expression((1, variable) for variable in week.captain) == 1,
                name=f"captain_count_{offset}",
            )
            for index, candidate in enumerate(candidates):
                previous_squad: int | highs_var = (
                    (1 if candidate.initial_member is not None else 0)
                    if offset == 0
                    else variables[offset - 1].squad[index]
                )
                model.addConstr(
                    week.squad[index]
                    == previous_squad + week.transfer_in[index] - week.transfer_out[index],
                    name=f"squad_transition_{offset}_{index}",
                )
                model.addConstr(
                    week.transfer_in[index] + week.transfer_out[index] <= 1,
                    name=f"no_same_week_churn_{offset}_{index}",
                )
                model.addConstr(
                    week.starter[index] <= week.squad[index],
                    name=f"starter_in_squad_{offset}_{index}",
                )
                model.addConstr(
                    week.captain[index] <= week.starter[index],
                    name=f"captain_starts_{offset}_{index}",
                )
                if candidate.player.id in request.excluded_players:
                    model.addConstr(
                        week.squad[index] == 0,
                        name=f"excluded_{offset}_{index}",
                    )

                if offset == 0:
                    if candidate.initial_member is None:
                        model.addConstr(
                            week.legacy_owned[index] == 0,
                            name=f"no_initial_legacy_{index}",
                        )
                        model.addConstr(
                            week.legacy_out[index] == 0,
                            name=f"no_initial_legacy_out_{index}",
                        )
                    else:
                        model.addConstr(
                            week.legacy_out[index] == week.transfer_out[index],
                            name=f"initial_legacy_out_{index}",
                        )
                        model.addConstr(
                            week.legacy_owned[index] == 1 - week.legacy_out[index],
                            name=f"initial_legacy_carry_{index}",
                        )
                else:
                    previous_legacy = variables[offset - 1].legacy_owned[index]
                    model.addConstr(
                        week.legacy_out[index] <= previous_legacy,
                        name=f"legacy_out_owned_{offset}_{index}",
                    )
                    model.addConstr(
                        week.legacy_out[index] <= week.transfer_out[index],
                        name=f"legacy_out_transfer_{offset}_{index}",
                    )
                    model.addConstr(
                        week.legacy_out[index] >= previous_legacy + week.transfer_out[index] - 1,
                        name=f"legacy_out_and_{offset}_{index}",
                    )
                    model.addConstr(
                        week.legacy_owned[index] == previous_legacy - week.legacy_out[index],
                        name=f"legacy_carry_{offset}_{index}",
                    )

            for position, quota in _SQUAD_QUOTAS.items():
                indices = [
                    index
                    for index, candidate in enumerate(candidates)
                    if candidate.player.position is position
                ]
                model.addConstr(
                    _expression((1, week.squad[index]) for index in indices) == quota,
                    name=f"squad_{position.value}_{offset}",
                )
                model.addConstr(
                    _expression((1, week.transfer_in[index]) for index in indices)
                    == _expression((1, week.transfer_out[index]) for index in indices),
                    name=f"position_balance_{position.value}_{offset}",
                )
            for position, (minimum, maximum) in _STARTER_BOUNDS.items():
                starters = _expression(
                    (1, week.starter[index])
                    for index, candidate in enumerate(candidates)
                    if candidate.player.position is position
                )
                model.addConstr(
                    starters >= minimum,
                    name=f"starter_{position.value}_min_{offset}",
                )
                model.addConstr(
                    starters <= maximum,
                    name=f"starter_{position.value}_max_{offset}",
                )
            for team_id in sorted({candidate.player.team_id for candidate in candidates}, key=str):
                model.addConstr(
                    _expression(
                        (1, week.squad[index])
                        for index, candidate in enumerate(candidates)
                        if candidate.player.team_id == team_id
                    )
                    <= 3,
                    name=f"club_{team_id}_{offset}",
                )

            bank_before: int | highs_var = (
                request.initial_manager_state.bank.tenths_million
                if offset == 0
                else variables[offset - 1].bank_after
            )
            sale_revenue = highs_linear_expression()
            buy_cost = highs_linear_expression()
            for index, candidate in enumerate(candidates):
                market_price = candidate.player.price.tenths_million
                sale_revenue += market_price * week.transfer_out[index]
                if candidate.initial_member is not None:
                    manager_selling = candidate.initial_member.selling_price
                    assert manager_selling is not None
                    sale_revenue += (
                        manager_selling.tenths_million - market_price
                    ) * week.legacy_out[index]
                buy_cost += market_price * week.transfer_in[index]
            model.addConstr(
                week.bank_after == bank_before + sale_revenue - buy_cost,
                name=f"bank_transition_{offset}",
            )

    @staticmethod
    def _run_or_raise(model: Highs, stage: str) -> None:
        run_status = model.run()
        status = model.getModelStatus()
        if run_status == HighsStatus.kOk and status == HighsModelStatus.kOptimal:
            return
        status_name = model.modelStatusToString(status)
        message = f"HiGHS multi-gameweek {stage} solve did not reach optimal: {status_name}"
        raise OptimisationError(
            message,
            code=(
                OptimisationErrorCode.INFEASIBLE
                if status == HighsModelStatus.kInfeasible
                else OptimisationErrorCode.SOLVER_FAILURE
            ),
            diagnostics=(_diagnostic(f"{stage}_solve_status", message, status=status_name),),
            solver_status=status_name,
        )

    def _build_result(
        self,
        model: Highs,
        candidates: tuple[_Candidate, ...],
        variables: tuple[_WeekVariables, ...],
        request: MultiGameweekPlanningRequest,
        hold: tuple[PlannedGameweek, ...],
        primary_objective: float,
        secondary_objective: float,
        primary_info: HighsInfo,
        tolerance: float,
        runtime: float,
    ) -> MultiGameweekPlanningResult:
        values = model.getSolution().col_value
        status = model.modelStatusToString(model.getModelStatus())
        owned_members = {
            member.player_id: member for member in request.initial_manager_state.squad.members
        }
        planned: list[PlannedGameweek] = []
        for offset, week in enumerate(variables):
            selected = self._chosen(week.squad, values)
            starters = self._chosen(week.starter, values)
            captains = self._chosen(week.captain, values)
            ins = self._chosen(week.transfer_in, values)
            outs = self._chosen(week.transfer_out, values)
            legacy = self._chosen(week.legacy_owned, values)
            legacy_outs = self._chosen(week.legacy_out, values)
            if not (
                len(selected) == 15
                and len(starters) == 11
                and len(captains) == 1
                and len(ins) == len(outs)
            ):
                raise OptimisationError(
                    "HiGHS returned incomplete multi-gameweek decision values",
                    code=OptimisationErrorCode.SOLVER_FAILURE,
                    solver_status=status,
                )
            captain_index = next(iter(captains))
            vice_index = min(
                starters - {captain_index},
                key=lambda index: (
                    -candidates[index].projections[offset].expected_points,
                    str(candidates[index].player.id),
                ),
            )
            ordered_starters = tuple(
                candidates[index].player.id
                for index in sorted(
                    starters,
                    key=lambda index: (
                        _POSITION_ORDER[candidates[index].player.position],
                        -candidates[index].projections[offset].expected_points,
                        str(candidates[index].player.id),
                    ),
                )
            )
            reserve = [
                index
                for index in selected - starters
                if candidates[index].player.position is Position.GOALKEEPER
            ]
            outfield = sorted(
                (
                    index
                    for index in selected - starters
                    if candidates[index].player.position is not Position.GOALKEEPER
                ),
                key=lambda index: (
                    -candidates[index].projections[offset].expected_points,
                    str(candidates[index].player.id),
                ),
            )
            if len(reserve) != 1 or len(outfield) != 3:
                raise OptimisationError(
                    "multi-gameweek solution has an invalid bench",
                    code=OptimisationErrorCode.SOLVER_FAILURE,
                    solver_status=status,
                )
            bench = tuple(candidates[index].player.id for index in (reserve + outfield))
            members = tuple(
                (
                    owned_members[candidates[index].player.id]
                    if index in legacy
                    else SquadMember(
                        player_id=candidates[index].player.id,
                        team_id=candidates[index].player.team_id,
                        position=candidates[index].player.position,
                        purchase_price=candidates[index].player.price,
                        selling_price=candidates[index].player.price,
                    )
                )
                for index in sorted(selected, key=lambda item: str(candidates[item].player.id))
            )
            squad = Squad(members=members)
            transfers = self._pair_transfers(candidates, outs, ins, legacy_outs)
            transfer_count = len(ins)
            free_transfers = round(values[week.free_transfers.index])
            paid_transfers = round(values[week.paid_transfers.index])
            next_free_transfers = round(values[week.next_free_transfers.index])
            bank_after = round(values[week.bank_after.index])
            bank_before = (
                request.initial_manager_state.bank.tenths_million
                if offset == 0
                else planned[-1].bank_after.tenths_million
            )
            gross = (
                sum(candidates[index].projections[offset].expected_points for index in starters)
                + candidates[captain_index].projections[offset].expected_points
            )
            net = gross - 4 * paid_transfers
            weight = request.resolved_weights[offset]
            squad_points = sum(
                candidates[index].projections[offset].expected_points for index in selected
            )
            formation_counts = Counter(candidates[index].player.position for index in starters)
            planned.append(
                PlannedGameweek(
                    gameweek=request.target_gameweeks[offset],
                    squad=squad,
                    starting_xi=ordered_starters,
                    captain_id=candidates[captain_index].player.id,
                    vice_captain_id=candidates[vice_index].player.id,
                    bench=bench,
                    formation=Formation(
                        defenders=formation_counts[Position.DEFENDER],
                        midfielders=formation_counts[Position.MIDFIELDER],
                        forwards=formation_counts[Position.FORWARD],
                    ),
                    transfers=transfers,
                    transfer_count=transfer_count,
                    free_transfers_available=free_transfers,
                    free_transfers_used=min(transfer_count, free_transfers),
                    paid_transfers=paid_transfers,
                    hit_cost=4 * paid_transfers,
                    bank_before=Money(tenths_million=bank_before),
                    bank_after=Money(tenths_million=bank_after),
                    gross_expected_score=gross,
                    net_expected_score=net,
                    discount_weight=weight,
                    weighted_contribution=weight * net,
                    next_free_transfers=next_free_transfers,
                    squad_expected_points=squad_points,
                )
            )

        realised_primary = sum(item.weighted_contribution for item in planned)
        realised_secondary = sum(
            item.discount_weight * item.squad_expected_points for item in planned
        )
        if abs(realised_primary - primary_objective) > tolerance * 1.01:
            raise OptimisationError(
                "extracted trajectory reduced the primary optimum",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=status,
            )
        if abs(realised_secondary - secondary_objective) > 1e-6:
            raise OptimisationError(
                "extracted trajectory differs from the stage-three objective",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=status,
            )
        hold_score = sum(item.weighted_contribution for item in hold)
        weighted_gross = sum(item.discount_weight * item.gross_expected_score for item in planned)
        weighted_hits = sum(item.discount_weight * item.hit_cost for item in planned)
        return MultiGameweekPlanningResult(
            horizon=request.horizon,
            gameweeks=tuple(planned),
            hold_trajectory=hold,
            total_transfers=sum(item.transfer_count for item in planned),
            total_weighted_gross_score=weighted_gross,
            total_weighted_hit_cost=weighted_hits,
            primary_objective=realised_primary,
            secondary_squad_objective=realised_secondary,
            hold_baseline_score=hold_score,
            weighted_expected_gain=realised_primary - hold_score,
            solver_name=f"HiGHS {model.version()}",
            solver_status=status,
            runtime_seconds=runtime,
            objective_bound=_finite_or_none(primary_info.mip_dual_bound),
            mip_gap=_finite_or_none(primary_info.mip_gap),
            diagnostics=(
                _diagnostic(
                    "joint_model",
                    "all gameweek transfer and squad states were solved jointly",
                    candidates=len(candidates),
                    horizon=request.horizon,
                    variables=model.getNumCol(),
                    constraints=model.getNumRow(),
                ),
                _diagnostic(
                    "primary_solve",
                    "stage one maximised weighted net horizon points",
                    objective=primary_objective,
                    existing_cost_sunk=request.initial_manager_state.existing_points_cost,
                ),
                _diagnostic(
                    "transfer_count_solve",
                    "stage two minimised total transfers at the primary optimum",
                    transfer_count=sum(item.transfer_count for item in planned),
                ),
                _diagnostic(
                    "squad_quality_solve",
                    "stage three maximised weighted squad quality after fixing earlier stages",
                    objective=secondary_objective,
                    primary_tolerance=tolerance,
                ),
                _diagnostic(
                    "frozen_prices",
                    "decision-time prices were frozen across the horizon",
                ),
                _diagnostic(
                    "hold_baseline",
                    "the initial squad was held while XI and captain were re-optimised",
                    objective=hold_score,
                ),
            ),
        )

    @staticmethod
    def _chosen(variables: tuple[highs_var, ...], values: Sequence[float]) -> set[int]:
        return {index for index, variable in enumerate(variables) if values[variable.index] > 0.5}

    @staticmethod
    def _pair_transfers(
        candidates: tuple[_Candidate, ...],
        outs: set[int],
        ins: set[int],
        legacy_outs: set[int],
    ) -> tuple[TransferPair, ...]:
        pairs: list[TransferPair] = []
        for position in Position:
            position_outs = sorted(
                (index for index in outs if candidates[index].player.position is position),
                key=lambda index: str(candidates[index].player.id),
            )
            position_ins = sorted(
                (index for index in ins if candidates[index].player.position is position),
                key=lambda index: str(candidates[index].player.id),
            )
            if len(position_outs) != len(position_ins):
                raise OptimisationError(
                    "multi-gameweek transfer solution is not position-balanced",
                    code=OptimisationErrorCode.SOLVER_FAILURE,
                )
            for out_index, in_index in zip(position_outs, position_ins, strict=True):
                candidate = candidates[out_index]
                if out_index in legacy_outs:
                    assert candidate.initial_member is not None
                    selling_price = candidate.initial_member.selling_price
                    assert selling_price is not None
                else:
                    selling_price = candidate.player.price
                pairs.append(
                    TransferPair(
                        player_out_id=candidate.player.id,
                        player_in_id=candidates[in_index].player.id,
                        position=position,
                        selling_price=selling_price,
                        buying_price=candidates[in_index].player.price,
                    )
                )
        return tuple(pairs)
