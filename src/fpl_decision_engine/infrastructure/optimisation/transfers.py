"""Direct HiGHS single-gameweek transfer optimisation."""

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
    SingleGameweekTransferOptimisationRequest,
    SingleGameweekTransferOptimisationResult,
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
    projection: Projection
    initially_owned: bool
    selling_price: Money | None


@dataclass(frozen=True)
class _Variables:
    final: tuple[highs_var, ...]
    starter: tuple[highs_var, ...]
    captain: tuple[highs_var, ...]
    transfer_in: tuple[highs_var, ...]
    transfer_out: tuple[highs_var, ...]
    paid_transfers: highs_var


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


class HighsSingleGameweekTransferOptimiser:
    """Optimise incremental normal transfers with lexicographic current-GW policy.

    Stage one maximises unconditional XI points plus a captain copy minus four points
    per transfer beyond the remaining free allowance. Stage two minimises transfer
    count at that optimum, preserving the option to roll. Stage three maximises final
    squad quality only after both current-GW criteria are fixed.
    """

    @property
    def engine_id(self) -> str:
        return "highs-single-gameweek-transfers-v1"

    def optimise(
        self, request: SingleGameweekTransferOptimisationRequest
    ) -> SingleGameweekTransferOptimisationResult:
        try:
            candidates = self._prepare_candidates(request)
            baseline = self._do_nothing_score(candidates, request)
            return self._solve(candidates, request, baseline)
        except OptimisationError:
            raise
        except Exception as exc:
            raise OptimisationError(
                "HiGHS transfer optimisation failed unexpectedly",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                diagnostics=(
                    _diagnostic(
                        "solver_exception",
                        "unexpected transfer solver failure",
                        type=type(exc).__name__,
                    ),
                ),
            ) from exc

    def _prepare_candidates(
        self, request: SingleGameweekTransferOptimisationRequest
    ) -> tuple[_Candidate, ...]:
        players: dict[UUID, Player] = {}
        for player in request.players:
            if player.id in players:
                _fail_input("duplicate candidate player", "duplicate_player", player_id=player.id)
            players[player.id] = player

        projections: dict[UUID, Projection] = {}
        for projection in request.projections:
            if projection.gameweek != request.target_gameweek:
                _fail_input(
                    "projection targets the wrong gameweek",
                    "wrong_gameweek",
                    player_id=projection.player_id,
                )
            if projection.player_id not in players:
                _fail_input(
                    "projection references an unknown candidate",
                    "unknown_projection_player",
                    player_id=projection.player_id,
                )
            if projection.player_id in projections:
                _fail_input(
                    "duplicate candidate projection",
                    "duplicate_projection",
                    player_id=projection.player_id,
                )
            projections[projection.player_id] = projection
        missing = players.keys() - projections.keys()
        if missing:
            _fail_input(
                "every candidate requires one target-gameweek projection",
                "missing_projection",
                player_ids=",".join(sorted(map(str, missing))),
            )

        owned_members = {member.player_id: member for member in request.manager_state.squad.members}
        missing_owned = owned_members.keys() - players.keys()
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
        for player_id, member in owned_members.items():
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
                projection=projections[player_id],
                initially_owned=player_id in owned_members,
                selling_price=(
                    owned_members[player_id].selling_price if player_id in owned_members else None
                ),
            )
            for player_id in sorted(players, key=str)
        )

    def _do_nothing_score(
        self,
        candidates: tuple[_Candidate, ...],
        request: SingleGameweekTransferOptimisationRequest,
    ) -> float:
        owned = tuple(candidate for candidate in candidates if candidate.initially_owned)
        budget = Money(
            tenths_million=sum(candidate.player.price.tenths_million for candidate in owned)
        )
        result = HighsSingleGameweekOptimiser().optimise(
            SingleGameweekOptimisationRequest(
                target_gameweek=request.target_gameweek,
                players=tuple(candidate.player for candidate in owned),
                projections=tuple(candidate.projection for candidate in owned),
                budget=budget,
                captain_fallback=False,
            )
        )
        return result.primary_objective

    def _solve(
        self,
        candidates: tuple[_Candidate, ...],
        request: SingleGameweekTransferOptimisationRequest,
        baseline: float,
    ) -> SingleGameweekTransferOptimisationResult:
        model = _configured_model()
        size = len(candidates)
        variables = _Variables(
            final=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"final_{i}")
                for i in range(size)
            ),
            starter=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"starter_{i}")
                for i in range(size)
            ),
            captain=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"captain_{i}")
                for i in range(size)
            ),
            transfer_in=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"in_{i}")
                for i in range(size)
            ),
            transfer_out=tuple(
                model.addVariable(lb=0, ub=1, type=HighsVarType.kInteger, name=f"out_{i}")
                for i in range(size)
            ),
            paid_transfers=model.addVariable(
                lb=0,
                ub=request.max_transfers,
                type=HighsVarType.kInteger,
                name="paid_transfers",
            ),
        )
        self._add_constraints(model, candidates, variables, request)

        gross = _expression(
            (candidate.projection.expected_points, variables.starter[index])
            for index, candidate in enumerate(candidates)
        )
        gross += _expression(
            (candidate.projection.expected_points, variables.captain[index])
            for index, candidate in enumerate(candidates)
        )
        primary = gross - 4 * variables.paid_transfers
        transfer_count = _expression((1, variable) for variable in variables.transfer_in)
        squad_quality = _expression(
            (candidate.projection.expected_points, variables.final[index])
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

        model.minimize(transfer_count)
        self._run_or_raise(model, "transfer_count")
        optimal_transfer_count = round(model.getObjectiveValue())
        model.addConstr(transfer_count == optimal_transfer_count, name="preserve_transfer_count")
        optimal_paid_transfers = max(
            0, optimal_transfer_count - request.manager_state.free_transfers
        )
        model.addConstr(
            variables.paid_transfers == optimal_paid_transfers,
            name="preserve_paid_transfers",
        )

        model.maximize(squad_quality)
        self._run_or_raise(model, "squad_quality")
        runtime = perf_counter() - started_at
        return self._build_result(
            candidates,
            request,
            variables,
            model.getSolution().col_value,
            primary_objective,
            model.getObjectiveValue(),
            baseline,
            primary_info,
            tolerance,
            runtime,
            model.modelStatusToString(model.getModelStatus()),
            model.version(),
        )

    def _add_constraints(
        self,
        model: Highs,
        candidates: tuple[_Candidate, ...],
        variables: _Variables,
        request: SingleGameweekTransferOptimisationRequest,
    ) -> None:
        transfer_count = _expression((1, variable) for variable in variables.transfer_in)
        model.addConstr(transfer_count <= request.max_transfers, name="maximum_transfers")
        model.addConstr(
            transfer_count == _expression((1, variable) for variable in variables.transfer_out),
            name="balanced_transfers",
        )
        model.addConstr(
            variables.paid_transfers >= transfer_count - request.manager_state.free_transfers,
            name="paid_transfer_lower",
        )
        model.addConstr(
            variables.paid_transfers <= transfer_count,
            name="paid_transfer_upper",
        )

        model.addConstr(
            _expression((1, variable) for variable in variables.final) == 15,
            name="final_squad_size",
        )
        model.addConstr(
            _expression((1, variable) for variable in variables.starter) == 11,
            name="starting_xi",
        )
        model.addConstr(
            _expression((1, variable) for variable in variables.captain) == 1,
            name="captain_count",
        )
        for index, candidate in enumerate(candidates):
            initial = 1 if candidate.initially_owned else 0
            model.addConstr(
                variables.final[index]
                == initial + variables.transfer_in[index] - variables.transfer_out[index],
                name=f"transfer_link_{index}",
            )
            if candidate.initially_owned:
                model.addConstr(variables.transfer_in[index] == 0, name=f"owned_no_in_{index}")
            else:
                model.addConstr(variables.transfer_out[index] == 0, name=f"unowned_no_out_{index}")
            model.addConstr(
                variables.starter[index] <= variables.final[index],
                name=f"starter_final_{index}",
            )
            model.addConstr(
                variables.captain[index] <= variables.starter[index],
                name=f"captain_starts_{index}",
            )
            if candidate.player.id in request.excluded_players:
                model.addConstr(variables.final[index] == 0, name=f"excluded_{index}")

        for position, quota in _SQUAD_QUOTAS.items():
            indices = [
                index
                for index, candidate in enumerate(candidates)
                if candidate.player.position is position
            ]
            model.addConstr(
                _expression((1, variables.final[index]) for index in indices) == quota,
                name=f"final_{position.value}",
            )
            model.addConstr(
                _expression((1, variables.transfer_in[index]) for index in indices)
                == _expression((1, variables.transfer_out[index]) for index in indices),
                name=f"balanced_{position.value}",
            )
        for position, (minimum, maximum) in _STARTER_BOUNDS.items():
            starters = _expression(
                (1, variables.starter[index])
                for index, candidate in enumerate(candidates)
                if candidate.player.position is position
            )
            model.addConstr(starters >= minimum, name=f"starter_{position.value}_min")
            model.addConstr(starters <= maximum, name=f"starter_{position.value}_max")
        for team_id in sorted({candidate.player.team_id for candidate in candidates}, key=str):
            model.addConstr(
                _expression(
                    (1, variables.final[index])
                    for index, candidate in enumerate(candidates)
                    if candidate.player.team_id == team_id
                )
                <= 3,
                name=f"club_{team_id}",
            )

        bank_after = request.manager_state.bank.tenths_million
        bank_after += _expression(
            (candidate.selling_price.tenths_million, variables.transfer_out[index])
            for index, candidate in enumerate(candidates)
            if candidate.selling_price is not None
        )
        bank_after -= _expression(
            (candidate.player.price.tenths_million, variables.transfer_in[index])
            for index, candidate in enumerate(candidates)
        )
        model.addConstr(bank_after >= 0, name="non_negative_bank")

    @staticmethod
    def _run_or_raise(model: Highs, stage: str) -> None:
        run_status = model.run()
        status = model.getModelStatus()
        if run_status == HighsStatus.kOk and status == HighsModelStatus.kOptimal:
            return
        status_name = model.modelStatusToString(status)
        message = f"HiGHS transfer {stage} solve did not reach optimal: {status_name}"
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
        candidates: tuple[_Candidate, ...],
        request: SingleGameweekTransferOptimisationRequest,
        variables: _Variables,
        values: Sequence[float],
        primary_objective: float,
        squad_quality: float,
        baseline: float,
        primary_info: HighsInfo,
        tolerance: float,
        runtime: float,
        solver_status: str,
        solver_version: str,
    ) -> SingleGameweekTransferOptimisationResult:
        def chosen(variables_: tuple[highs_var, ...]) -> set[int]:
            return {
                index for index, variable in enumerate(variables_) if values[variable.index] > 0.5
            }

        selected = chosen(variables.final)
        starters = chosen(variables.starter)
        ins = chosen(variables.transfer_in)
        outs = chosen(variables.transfer_out)
        captains = chosen(variables.captain)
        if not (
            len(selected) == 15
            and len(starters) == 11
            and len(captains) == 1
            and len(ins) == len(outs)
        ):
            raise OptimisationError(
                "HiGHS returned incomplete transfer decision values",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=solver_status,
            )
        captain_index = next(iter(captains))
        vice_index = min(
            starters - {captain_index},
            key=lambda index: (
                -candidates[index].projection.expected_points,
                str(candidates[index].player.id),
            ),
        )
        ordered_starters = tuple(
            candidates[index].player.id
            for index in sorted(
                starters,
                key=lambda index: (
                    _POSITION_ORDER[candidates[index].player.position],
                    -candidates[index].projection.expected_points,
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
                -candidates[index].projection.expected_points,
                str(candidates[index].player.id),
            ),
        )
        if len(reserve) != 1 or len(outfield) != 3:
            raise OptimisationError(
                "transfer solution has an invalid bench",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=solver_status,
            )
        bench = tuple(candidates[index].player.id for index in (reserve + outfield))

        owned_members = {member.player_id: member for member in request.manager_state.squad.members}
        members = tuple(
            owned_members.get(
                candidates[index].player.id,
                SquadMember(
                    player_id=candidates[index].player.id,
                    team_id=candidates[index].player.team_id,
                    position=candidates[index].player.position,
                    purchase_price=candidates[index].player.price,
                    selling_price=candidates[index].player.price,
                ),
            )
            for index in sorted(selected, key=lambda item: str(candidates[item].player.id))
        )
        final_squad = Squad(members=members)
        transfer_pairs = self._pair_transfers(candidates, outs, ins)
        transfer_count = len(ins)
        free_used = min(transfer_count, request.manager_state.free_transfers)
        paid = transfer_count - free_used
        selling_total = 0
        for index in outs:
            selling_price = candidates[index].selling_price
            if selling_price is None:
                raise OptimisationError(
                    "owned transfer-out candidate has no selling price",
                    code=OptimisationErrorCode.SOLVER_FAILURE,
                    solver_status=solver_status,
                )
            selling_total += selling_price.tenths_million
        bank_after = request.manager_state.bank.tenths_million
        bank_after += selling_total
        bank_after -= sum(candidates[index].player.price.tenths_million for index in ins)
        gross = (
            sum(candidates[index].projection.expected_points for index in starters)
            + candidates[captain_index].projection.expected_points
        )
        net = gross - (4 * paid)
        realised_squad_quality = sum(
            candidates[index].projection.expected_points for index in selected
        )
        if abs(realised_squad_quality - squad_quality) > 1e-6:
            raise OptimisationError(
                "extracted squad score differs from stage-three objective",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=solver_status,
            )
        if abs(net - primary_objective) > tolerance * 1.01:
            raise OptimisationError(
                "extracted transfer plan reduced the primary optimum",
                code=OptimisationErrorCode.SOLVER_FAILURE,
                solver_status=solver_status,
            )
        formation_counts = Counter(candidates[index].player.position for index in starters)
        return SingleGameweekTransferOptimisationResult(
            transfers=transfer_pairs,
            final_squad=final_squad,
            starting_xi=ordered_starters,
            captain_id=candidates[captain_index].player.id,
            vice_captain_id=candidates[vice_index].player.id,
            bench=bench,
            formation=Formation(
                defenders=formation_counts[Position.DEFENDER],
                midfielders=formation_counts[Position.MIDFIELDER],
                forwards=formation_counts[Position.FORWARD],
            ),
            transfer_count=transfer_count,
            free_transfers_used=free_used,
            paid_transfers=paid,
            additional_points_cost=4 * paid,
            bank_before=request.manager_state.bank,
            bank_after=Money(tenths_million=bank_after),
            next_free_transfers=min(
                5,
                max(1, request.manager_state.free_transfers - transfer_count + 1),
            ),
            gross_expected_score=gross,
            net_expected_score=net,
            do_nothing_expected_score=baseline,
            expected_gain=net - baseline,
            final_squad_expected_points=squad_quality,
            solver_name=f"HiGHS {solver_version}",
            solver_status=solver_status,
            runtime_seconds=runtime,
            objective_bound=_finite_or_none(primary_info.mip_dual_bound),
            mip_gap=_finite_or_none(primary_info.mip_gap),
            diagnostics=(
                _diagnostic(
                    "primary_solve",
                    "stage one maximised current-GW score net of incremental hits",
                    objective=primary_objective,
                    existing_cost_sunk=request.manager_state.existing_points_cost,
                ),
                _diagnostic(
                    "transfer_count_solve",
                    "stage two minimised transfers without reducing primary objective",
                    transfer_count=transfer_count,
                ),
                _diagnostic(
                    "squad_quality_solve",
                    "stage three maximised final squad points after fixing earlier stages",
                    objective=squad_quality,
                    primary_tolerance=tolerance,
                ),
                _diagnostic(
                    "do_nothing_baseline",
                    "current squad scored with unchanged mean-only #6 semantics",
                    objective=baseline,
                ),
            ),
        )

    @staticmethod
    def _pair_transfers(
        candidates: tuple[_Candidate, ...], outs: set[int], ins: set[int]
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
                    "transfer solution is not position-balanced",
                    code=OptimisationErrorCode.SOLVER_FAILURE,
                )
            for out_index, in_index in zip(position_outs, position_ins, strict=True):
                selling_price = candidates[out_index].selling_price
                assert selling_price is not None
                pairs.append(
                    TransferPair(
                        player_out_id=candidates[out_index].player.id,
                        player_in_id=candidates[in_index].player.id,
                        position=position,
                        selling_price=selling_price,
                        buying_price=candidates[in_index].player.price,
                    )
                )
        return tuple(pairs)
