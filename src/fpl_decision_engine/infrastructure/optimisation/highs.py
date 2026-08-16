"""Direct HiGHS implementation of the single-gameweek optimisation contract."""

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
    OptimisationDiagnostic,
    Player,
    Position,
    Projection,
    SingleGameweekOptimisationRequest,
    SingleGameweekOptimisationResult,
    Squad,
    SquadMember,
)
from fpl_decision_engine.ports import OptimisationError, OptimisationErrorCode

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
    projection: Projection


@dataclass(frozen=True)
class _DecisionVariables:
    squad: tuple[highs_var, ...]
    starter: tuple[highs_var, ...]
    captain: tuple[highs_var, ...]


def _expression(terms: Iterable[tuple[float | int, highs_var]]) -> highs_linear_expression:
    expression = highs_linear_expression()
    for coefficient, variable in terms:
        expression += coefficient * variable
    return expression


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
        status = model.setOptionValue(option, value)
        if status != HighsStatus.kOk:
            raise RuntimeError(f"HiGHS rejected deterministic option {option!r}")
    return model


def _finite_or_none(value: float) -> float | None:
    return value if isfinite(value) else None


class HighsSingleGameweekOptimiser:
    """Solve the mean-only FPL baseline with deterministic lexicographic bench quality.

    Stage one maximises nominal XI points plus exactly one extra captain copy. Stage two
    fixes that optimum within a tight absolute tolerance and maximises total projected
    points of the selected squad. Appearance probabilities are evidence only and are not
    multiplied into already-unconditional expected points.
    """

    @property
    def engine_id(self) -> str:
        return "highs-single-gameweek-v1"

    def optimise(
        self, request: SingleGameweekOptimisationRequest
    ) -> SingleGameweekOptimisationResult:
        """Validate canonical inputs, solve both stages, and verify the extracted result."""

        try:
            candidates = self._prepare_candidates(request)
            minimum_cost = self._minimum_legal_squad_cost(candidates, request)
            if minimum_cost > request.budget.tenths_million:
                message = (
                    f"minimum legal squad cost {minimum_cost} tenths exceeds budget "
                    f"{request.budget.tenths_million} tenths"
                )
                raise OptimisationError(
                    message,
                    code=OptimisationErrorCode.INFEASIBLE,
                    diagnostics=(
                        _diagnostic(
                            "budget_below_minimum",
                            message,
                            minimum_cost_tenths=minimum_cost,
                            budget_tenths=request.budget.tenths_million,
                        ),
                    ),
                )
            return self._solve(candidates, request, minimum_cost)
        except OptimisationError:
            raise
        except Exception as exc:
            raise OptimisationError(
                "HiGHS optimisation failed unexpectedly",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                diagnostics=(
                    _diagnostic(
                        "solver_exception", "unexpected solver failure", type=type(exc).__name__
                    ),
                ),
            ) from exc

    def _prepare_candidates(
        self, request: SingleGameweekOptimisationRequest
    ) -> tuple[_Candidate, ...]:
        players_by_id: dict[UUID, Player] = {}
        for player in request.players:
            if player.id in players_by_id:
                _fail_input(
                    f"duplicate candidate player {player.id}",
                    "duplicate_player",
                    player_id=player.id,
                )
            players_by_id[player.id] = player

        projections_by_id: dict[UUID, Projection] = {}
        for projection in request.projections:
            if projection.gameweek != request.target_gameweek:
                _fail_input(
                    f"projection for player {projection.player_id} targets gameweek "
                    f"{projection.gameweek.value}, expected {request.target_gameweek.value}",
                    "wrong_gameweek",
                    player_id=projection.player_id,
                    projection_gameweek=projection.gameweek.value,
                    target_gameweek=request.target_gameweek.value,
                )
            if projection.player_id not in players_by_id:
                _fail_input(
                    f"projection references unknown candidate player {projection.player_id}",
                    "unknown_projection_player",
                    player_id=projection.player_id,
                )
            if projection.player_id in projections_by_id:
                _fail_input(
                    f"duplicate projection for player {projection.player_id}",
                    "duplicate_projection",
                    player_id=projection.player_id,
                )
            projections_by_id[projection.player_id] = projection

        missing = players_by_id.keys() - projections_by_id.keys()
        if missing:
            _fail_input(
                "every candidate must have exactly one target-gameweek projection",
                "missing_projection",
                player_ids=",".join(sorted(str(player_id) for player_id in missing)),
            )

        self._validate_scenarios(request, players_by_id)
        available = players_by_id.keys() - request.excluded_players
        if len(available) < 15:
            _fail_input(
                f"fewer than 15 usable candidates remain ({len(available)})",
                "insufficient_candidates",
                available=len(available),
            )
        available_positions = Counter(players_by_id[player_id].position for player_id in available)
        for position, required in _SQUAD_QUOTAS.items():
            actual = available_positions[position]
            if actual < required:
                _fail_input(
                    f"insufficient {position.value} candidates: need {required}, found {actual}",
                    "insufficient_position",
                    position=position.value,
                    required=required,
                    available=actual,
                )

        return tuple(
            _Candidate(players_by_id[player_id], projections_by_id[player_id])
            for player_id in sorted(players_by_id, key=str)
        )

    def _validate_scenarios(
        self, request: SingleGameweekOptimisationRequest, players_by_id: dict[UUID, Player]
    ) -> None:
        scenario_ids = (
            request.must_include_in_squad
            | request.excluded_players
            | request.forced_starters
            | frozenset(
                player_id
                for player_id in (request.forced_captain, request.forced_vice_captain)
                if player_id is not None
            )
        )
        unknown = scenario_ids - players_by_id.keys()
        if unknown:
            _fail_input(
                "scenario references players absent from the candidate set",
                "unknown_scenario_player",
                player_ids=",".join(sorted(str(player_id) for player_id in unknown)),
            )
        overlap = request.must_include_in_squad & request.excluded_players
        if overlap:
            _fail_input(
                "players cannot be both included and excluded",
                "include_exclude_contradiction",
                player_ids=",".join(sorted(str(player_id) for player_id in overlap)),
            )
        forced_leaders = frozenset(
            player_id
            for player_id in (request.forced_captain, request.forced_vice_captain)
            if player_id is not None
        )
        forced_xi = request.forced_starters | forced_leaders
        excluded_forced = forced_xi & request.excluded_players
        if excluded_forced:
            _fail_input(
                "forced starters or leaders cannot be excluded",
                "forced_excluded_contradiction",
                player_ids=",".join(sorted(str(player_id) for player_id in excluded_forced)),
            )
        if (
            request.forced_captain is not None
            and request.forced_captain == request.forced_vice_captain
        ):
            _fail_input(
                "forced captain and vice-captain must differ",
                "captain_vice_contradiction",
                player_id=request.forced_captain,
            )
        if len(forced_xi) > 11:
            _fail_input(
                f"too many forced starters ({len(forced_xi)}; maximum 11)",
                "too_many_forced_starters",
                forced_starters=len(forced_xi),
            )

        forced_squad = request.must_include_in_squad | forced_xi
        if len(forced_squad) > 15:
            _fail_input(
                f"too many forced squad players ({len(forced_squad)}; maximum 15)",
                "too_many_forced_squad_players",
                forced_squad=len(forced_squad),
            )
        forced_squad_positions = Counter(
            players_by_id[player_id].position for player_id in forced_squad
        )
        for position, count in forced_squad_positions.items():
            if count > _SQUAD_QUOTAS[position]:
                _fail_input(
                    f"forced squad contains too many {position.value} players",
                    "forced_squad_position",
                    position=position.value,
                    forced=count,
                    maximum=_SQUAD_QUOTAS[position],
                )
        forced_clubs = Counter(players_by_id[player_id].team_id for player_id in forced_squad)
        if any(count > 3 for count in forced_clubs.values()):
            _fail_input(
                "forced squad would exceed three players from one club",
                "forced_squad_club_limit",
            )

        forced_xi_positions = Counter(players_by_id[player_id].position for player_id in forced_xi)
        legal_completion_exists = any(
            forced_xi_positions[Position.GOALKEEPER] <= 1
            and forced_xi_positions[Position.DEFENDER] <= defenders
            and forced_xi_positions[Position.MIDFIELDER] <= midfielders
            and forced_xi_positions[Position.FORWARD] <= forwards
            for defenders in range(3, 6)
            for midfielders in range(2, 6)
            for forwards in range(1, 4)
            if defenders + midfielders + forwards == 10
        )
        if not legal_completion_exists:
            _fail_input(
                "forced starters cannot be completed to a legal formation",
                "forced_starter_formation",
            )

    def _minimum_legal_squad_cost(
        self,
        candidates: tuple[_Candidate, ...],
        request: SingleGameweekOptimisationRequest,
    ) -> int:
        model = _configured_model()
        squad = tuple(
            model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"min_squad_{index}")
            for index in range(len(candidates))
        )
        self._add_squad_constraints(model, candidates, squad, request, include_budget=False)
        model.minimize(
            _expression(
                (candidate.player.price.tenths_million, squad[index])
                for index, candidate in enumerate(candidates)
            )
        )
        run_status = model.run()
        status = model.getModelStatus()
        status_name = model.modelStatusToString(status)
        if run_status != HighsStatus.kOk or status != HighsModelStatus.kOptimal:
            message = "no legal squad satisfies candidate, club, position and scenario constraints"
            raise OptimisationError(
                message,
                code=(
                    OptimisationErrorCode.INFEASIBLE
                    if status == HighsModelStatus.kInfeasible
                    else OptimisationErrorCode.SOLVER_FAILURE
                ),
                diagnostics=(_diagnostic("minimum_cost_status", message, status=status_name),),
                solver_status=status_name,
            )
        return round(model.getObjectiveValue())

    def _add_squad_constraints(
        self,
        model: Highs,
        candidates: tuple[_Candidate, ...],
        squad: tuple[highs_var, ...],
        request: SingleGameweekOptimisationRequest,
        *,
        include_budget: bool,
    ) -> None:
        model.addConstr(_expression((1, variable) for variable in squad) == 15, name="squad_size")
        for position, quota in _SQUAD_QUOTAS.items():
            model.addConstr(
                _expression(
                    (1, squad[index])
                    for index, candidate in enumerate(candidates)
                    if candidate.player.position is position
                )
                == quota,
                name=f"squad_{position.value}",
            )
        team_ids = sorted({candidate.player.team_id for candidate in candidates}, key=str)
        for team_id in team_ids:
            model.addConstr(
                _expression(
                    (1, squad[index])
                    for index, candidate in enumerate(candidates)
                    if candidate.player.team_id == team_id
                )
                <= 3,
                name=f"club_{team_id}",
            )
        if include_budget:
            model.addConstr(
                _expression(
                    (candidate.player.price.tenths_million, squad[index])
                    for index, candidate in enumerate(candidates)
                )
                <= request.budget.tenths_million,
                name="budget",
            )

        forced_squad = (
            request.must_include_in_squad
            | request.forced_starters
            | frozenset(
                player_id
                for player_id in (request.forced_captain, request.forced_vice_captain)
                if player_id is not None
            )
        )
        for index, candidate in enumerate(candidates):
            if candidate.player.id in forced_squad:
                model.addConstr(squad[index] == 1, name=f"force_squad_{index}")
            if candidate.player.id in request.excluded_players:
                model.addConstr(squad[index] == 0, name=f"exclude_{index}")

    def _solve(
        self,
        candidates: tuple[_Candidate, ...],
        request: SingleGameweekOptimisationRequest,
        minimum_cost: int,
    ) -> SingleGameweekOptimisationResult:
        model = _configured_model()
        variables = _DecisionVariables(
            squad=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"squad_{index}")
                for index in range(len(candidates))
            ),
            starter=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"starter_{index}")
                for index in range(len(candidates))
            ),
            captain=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"captain_{index}")
                for index in range(len(candidates))
            ),
        )
        self._add_squad_constraints(
            model, candidates, variables.squad, request, include_budget=True
        )
        self._add_lineup_constraints(model, candidates, variables, request)

        primary = _expression(
            (candidate.projection.expected_points, variables.starter[index])
            for index, candidate in enumerate(candidates)
        )
        primary += _expression(
            (candidate.projection.expected_points, variables.captain[index])
            for index, candidate in enumerate(candidates)
        )
        secondary = _expression(
            (candidate.projection.expected_points, variables.squad[index])
            for index, candidate in enumerate(candidates)
        )

        started_at = perf_counter()
        model.maximize(primary)
        self._run_or_raise(model, stage="primary")
        primary_objective = model.getObjectiveValue()
        primary_info = model.getInfo()

        tolerance = _PRIMARY_TOLERANCE * max(1.0, abs(primary_objective))
        model.addConstr(primary >= primary_objective - tolerance, name="preserve_primary_lower")
        model.addConstr(primary <= primary_objective + tolerance, name="preserve_primary_upper")
        model.maximize(secondary)
        self._run_or_raise(model, stage="secondary")
        runtime = perf_counter() - started_at
        secondary_objective = model.getObjectiveValue()
        status_name = model.modelStatusToString(model.getModelStatus())

        return self._build_result(
            candidates=candidates,
            request=request,
            variables=variables,
            column_values=model.getSolution().col_value,
            primary_objective=primary_objective,
            secondary_objective=secondary_objective,
            primary_info=primary_info,
            minimum_cost=minimum_cost,
            tolerance=tolerance,
            runtime=runtime,
            solver_status=status_name,
            solver_version=model.version(),
        )

    def _add_lineup_constraints(
        self,
        model: Highs,
        candidates: tuple[_Candidate, ...],
        variables: _DecisionVariables,
        request: SingleGameweekOptimisationRequest,
    ) -> None:
        model.addConstr(
            _expression((1, variable) for variable in variables.starter) == 11,
            name="starting_xi",
        )
        model.addConstr(
            _expression((1, variable) for variable in variables.captain) == 1,
            name="captain_count",
        )
        for position, (minimum, maximum) in _STARTER_BOUNDS.items():
            position_starters = _expression(
                (1, variables.starter[index])
                for index, candidate in enumerate(candidates)
                if candidate.player.position is position
            )
            model.addConstr(position_starters >= minimum, name=f"starter_{position.value}_min")
            model.addConstr(position_starters <= maximum, name=f"starter_{position.value}_max")
        for index, candidate in enumerate(candidates):
            model.addConstr(
                variables.starter[index] <= variables.squad[index],
                name=f"starter_in_squad_{index}",
            )
            model.addConstr(
                variables.captain[index] <= variables.starter[index],
                name=f"captain_starts_{index}",
            )
            if candidate.player.id in request.forced_starters:
                model.addConstr(variables.starter[index] == 1, name=f"force_starter_{index}")
            if candidate.player.id == request.forced_captain:
                model.addConstr(variables.captain[index] == 1, name=f"force_captain_{index}")
            if candidate.player.id == request.forced_vice_captain:
                model.addConstr(variables.starter[index] == 1, name=f"force_vice_starter_{index}")
                model.addConstr(
                    variables.captain[index] == 0, name=f"forced_vice_not_captain_{index}"
                )

    @staticmethod
    def _run_or_raise(model: Highs, *, stage: str) -> None:
        run_status = model.run()
        model_status = model.getModelStatus()
        if run_status == HighsStatus.kOk and model_status == HighsModelStatus.kOptimal:
            return
        status_name = model.modelStatusToString(model_status)
        message = f"HiGHS {stage} solve did not reach an optimal solution: {status_name}"
        raise OptimisationError(
            message,
            code=(
                OptimisationErrorCode.INFEASIBLE
                if model_status == HighsModelStatus.kInfeasible
                else OptimisationErrorCode.SOLVER_FAILURE
            ),
            diagnostics=(_diagnostic(f"{stage}_solve_status", message, status=status_name),),
            solver_status=status_name,
        )

    def _build_result(
        self,
        *,
        candidates: tuple[_Candidate, ...],
        request: SingleGameweekOptimisationRequest,
        variables: _DecisionVariables,
        column_values: Sequence[float],
        primary_objective: float,
        secondary_objective: float,
        primary_info: HighsInfo,
        minimum_cost: int,
        tolerance: float,
        runtime: float,
        solver_status: str,
        solver_version: str,
    ) -> SingleGameweekOptimisationResult:
        selected = {
            index
            for index, variable in enumerate(variables.squad)
            if column_values[variable.index] > 0.5
        }
        starters = {
            index
            for index, variable in enumerate(variables.starter)
            if column_values[variable.index] > 0.5
        }
        captains = [
            index
            for index, variable in enumerate(variables.captain)
            if column_values[variable.index] > 0.5
        ]
        if len(selected) != 15 or len(starters) != 11 or len(captains) != 1:
            raise OptimisationError(
                "HiGHS returned non-integral or incomplete decision values",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                diagnostics=(
                    _diagnostic(
                        "invalid_solution_cardinality",
                        "extracted solution cardinalities are invalid",
                        squad=len(selected),
                        starters=len(starters),
                        captains=len(captains),
                    ),
                ),
                solver_status=solver_status,
            )

        captain_index = captains[0]
        captain_id = candidates[captain_index].player.id
        vice_index = self._vice_captain_index(candidates, starters, captain_index, request)
        ordered_starters = tuple(
            candidates[index].player.id
            for index in sorted(starters, key=lambda item: self._lineup_key(candidates[item]))
        )
        bench_indices = selected - starters
        reserve_goalkeepers = [
            index
            for index in bench_indices
            if candidates[index].player.position is Position.GOALKEEPER
        ]
        outfield_bench = [
            index
            for index in bench_indices
            if candidates[index].player.position is not Position.GOALKEEPER
        ]
        if len(reserve_goalkeepers) != 1 or len(outfield_bench) != 3:
            raise OptimisationError(
                "extracted bench does not contain one goalkeeper and three outfield players",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=solver_status,
            )
        outfield_bench.sort(key=lambda item: self._points_key(candidates[item]))
        bench = tuple(
            candidates[index].player.id for index in (reserve_goalkeepers + outfield_bench)
        )

        members = tuple(
            SquadMember(
                player_id=candidates[index].player.id,
                team_id=candidates[index].player.team_id,
                position=candidates[index].player.position,
                purchase_price=candidates[index].player.price,
            )
            for index in sorted(selected, key=lambda item: str(candidates[item].player.id))
        )
        squad = Squad(members=members)
        cost = sum(candidates[index].player.price.tenths_million for index in selected)
        starter_positions = Counter(candidates[index].player.position for index in starters)
        formation = Formation(
            defenders=starter_positions[Position.DEFENDER],
            midfielders=starter_positions[Position.MIDFIELDER],
            forwards=starter_positions[Position.FORWARD],
        )

        realised_primary = (
            sum(candidates[index].projection.expected_points for index in starters)
            + candidates[captain_index].projection.expected_points
        )
        if abs(realised_primary - primary_objective) > tolerance * 1.01:
            raise OptimisationError(
                "secondary solve reduced the primary optimum",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                diagnostics=(
                    _diagnostic(
                        "primary_objective_drift",
                        "extracted primary score differs from stage-one optimum",
                        stage_one=primary_objective,
                        extracted=realised_primary,
                        tolerance=tolerance,
                    ),
                ),
                solver_status=solver_status,
            )
        realised_secondary = sum(candidates[index].projection.expected_points for index in selected)
        if abs(realised_secondary - secondary_objective) > 1e-6:
            raise OptimisationError(
                "extracted squad score differs from secondary objective",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=solver_status,
            )

        self._validate_scenario_solution(candidates, selected, starters, captain_id, request)
        return SingleGameweekOptimisationResult(
            squad=squad,
            starting_xi=ordered_starters,
            captain_id=captain_id,
            vice_captain_id=candidates[vice_index].player.id,
            bench=bench,
            formation=formation,
            squad_cost=Money(tenths_million=cost),
            bank_remaining=Money(tenths_million=request.budget.tenths_million - cost),
            primary_objective=primary_objective,
            secondary_squad_objective=secondary_objective,
            solver_name=f"HiGHS {solver_version}",
            solver_status=solver_status,
            runtime_seconds=runtime,
            objective_bound=_finite_or_none(primary_info.mip_dual_bound),
            mip_gap=_finite_or_none(primary_info.mip_gap),
            diagnostics=(
                _diagnostic(
                    "primary_solve",
                    "stage one maximised XI points plus one captain copy",
                    objective=primary_objective,
                    bound=primary_info.mip_dual_bound,
                    gap=primary_info.mip_gap,
                ),
                _diagnostic(
                    "secondary_solve",
                    "stage two maximised selected-squad points without reducing stage one",
                    objective=secondary_objective,
                    primary_tolerance=tolerance,
                ),
                _diagnostic(
                    "minimum_cost",
                    "minimum legal scenario-constrained squad cost",
                    tenths_million=minimum_cost,
                ),
            ),
        )

    @staticmethod
    def _points_key(candidate: _Candidate) -> tuple[float, str]:
        return -candidate.projection.expected_points, str(candidate.player.id)

    @staticmethod
    def _lineup_key(candidate: _Candidate) -> tuple[int, float, str]:
        return (
            _POSITION_ORDER[candidate.player.position],
            -candidate.projection.expected_points,
            str(candidate.player.id),
        )

    def _vice_captain_index(
        self,
        candidates: tuple[_Candidate, ...],
        starters: set[int],
        captain_index: int,
        request: SingleGameweekOptimisationRequest,
    ) -> int:
        if request.forced_vice_captain is not None:
            return next(
                index
                for index in starters
                if candidates[index].player.id == request.forced_vice_captain
            )
        return min(
            (index for index in starters if index != captain_index),
            key=lambda item: self._points_key(candidates[item]),
        )

    @staticmethod
    def _validate_scenario_solution(
        candidates: tuple[_Candidate, ...],
        selected: set[int],
        starters: set[int],
        captain_id: UUID,
        request: SingleGameweekOptimisationRequest,
    ) -> None:
        selected_ids = {candidates[index].player.id for index in selected}
        starter_ids = {candidates[index].player.id for index in starters}
        valid = (
            request.must_include_in_squad <= selected_ids
            and not request.excluded_players & selected_ids
            and request.forced_starters <= starter_ids
            and (request.forced_captain is None or request.forced_captain == captain_id)
            and (request.forced_vice_captain is None or request.forced_vice_captain in starter_ids)
        )
        if not valid:
            raise OptimisationError(
                "extracted solution violates requested scenario constraints",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status="Optimal",
            )
