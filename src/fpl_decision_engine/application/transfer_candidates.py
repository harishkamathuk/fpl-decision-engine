"""Application-layer enumeration and ranking of ROLL and one-free-transfer actions.

This module implements #114: it generates the manager's current squad as an
explicit ROLL candidate and every legal one-free-transfer alternative, then
evaluates each resulting fixed squad through the existing canonical single-GW
optimiser.  No core optimiser change or multi-transfer planning is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fpl_decision_engine.domain import (
    Formation,
    GameweekNumber,
    ManagerState,
    Money,
    OptimisationDiagnostic,
    Player,
    Projection,
    SingleGameweekOptimisationRequest,
    SingleGameweekOptimisationResult,
    Squad,
    SquadMember,
)
from fpl_decision_engine.ports import OptimisationEngine

_CANDIDATE_KINDS = ("ROLL", "TRANSFER")


@dataclass(frozen=True, slots=True)
class TransferCandidate:
    kind: str
    player_out_id: UUID | None
    player_in_id: UUID | None
    squad: Squad
    bank_before: Money
    bank_after: Money
    free_transfers_used: int

    def __post_init__(self) -> None:
        if self.kind not in _CANDIDATE_KINDS:
            raise ValueError(f"unknown candidate kind: {self.kind}")
        if self.kind == "ROLL":
            if self.player_out_id is not None or self.player_in_id is not None:
                raise ValueError("ROLL candidate must not specify player out or in")
            if self.free_transfers_used != 0:
                raise ValueError("ROLL candidate must consume zero free transfers")
        else:
            if self.player_out_id is None or self.player_in_id is None:
                raise ValueError("TRANSFER candidate must specify player out and in")
            if self.player_out_id == self.player_in_id:
                raise ValueError("TRANSFER candidate player out and in must differ")
            if self.free_transfers_used != 1:
                raise ValueError("TRANSFER candidate must consume exactly one free transfer")

    @property
    def identity(self) -> tuple[str, str]:
        if self.kind == "ROLL":
            return ("roll", "roll")
        return (str(self.player_out_id), str(self.player_in_id))


@dataclass(frozen=True, slots=True)
class EvaluatedTransferCandidate:
    candidate: TransferCandidate
    current_gw_score: float
    uplift_vs_roll: float
    optimisation_result: SingleGameweekOptimisationResult

    @property
    def starting_xi(self) -> tuple[UUID, ...]:
        return self.optimisation_result.starting_xi

    @property
    def bench(self) -> tuple[UUID, ...]:
        return self.optimisation_result.bench

    @property
    def captain_id(self) -> UUID:
        return self.optimisation_result.captain_id

    @property
    def vice_captain_id(self) -> UUID:
        return self.optimisation_result.vice_captain_id

    @property
    def formation(self) -> Formation:
        return self.optimisation_result.formation

    @property
    def solver_name(self) -> str:
        return self.optimisation_result.solver_name

    @property
    def solver_status(self) -> str:
        return self.optimisation_result.solver_status

    @property
    def runtime_seconds(self) -> float:
        return self.optimisation_result.runtime_seconds

    @property
    def objective_bound(self) -> float | None:
        return self.optimisation_result.objective_bound

    @property
    def mip_gap(self) -> float | None:
        return self.optimisation_result.mip_gap

    @property
    def diagnostics(self) -> tuple[OptimisationDiagnostic, ...]:
        return self.optimisation_result.diagnostics


def _build_roll_candidate(manager_state: ManagerState) -> TransferCandidate:
    return TransferCandidate(
        kind="ROLL",
        player_out_id=None,
        player_in_id=None,
        squad=manager_state.squad,
        bank_before=manager_state.bank,
        bank_after=manager_state.bank,
        free_transfers_used=0,
    )


def _build_transfer_candidate(
    manager_state: ManagerState,
    player_out_member: SquadMember,
    incoming: Player,
) -> TransferCandidate | None:
    out_id = player_out_member.player_id
    in_id = incoming.id

    if incoming.position is not player_out_member.position:
        return None

    owned_ids = {member.player_id for member in manager_state.squad.members}
    if in_id in owned_ids:
        return None

    assert player_out_member.selling_price is not None
    available_funds = Money(
        tenths_million=(
            manager_state.bank.tenths_million + player_out_member.selling_price.tenths_million
        )
    )
    if incoming.price.tenths_million > available_funds.tenths_million:
        return None

    new_members: list[SquadMember] = []
    for member in manager_state.squad.members:
        if member.player_id == out_id:
            new_members.append(
                SquadMember(
                    player_id=in_id,
                    team_id=incoming.team_id,
                    position=incoming.position,
                    purchase_price=incoming.price,
                    selling_price=None,
                )
            )
        else:
            new_members.append(member)

    try:
        resulting_squad = Squad(members=tuple(new_members))
    except ValueError:
        return None

    bank_after = Money(
        tenths_million=(available_funds.tenths_million - incoming.price.tenths_million)
    )

    return TransferCandidate(
        kind="TRANSFER",
        player_out_id=out_id,
        player_in_id=in_id,
        squad=resulting_squad,
        bank_before=manager_state.bank,
        bank_after=bank_after,
        free_transfers_used=1,
    )


def enumerate_transfer_candidates(
    manager_state: ManagerState,
    players: tuple[Player, ...],
    projections: tuple[Projection, ...],
) -> tuple[TransferCandidate, ...]:
    owned_ids = {member.player_id for member in manager_state.squad.members}
    projection_by_id = {proj.player_id: proj for proj in projections}

    candidates: list[TransferCandidate] = [_build_roll_candidate(manager_state)]

    if manager_state.free_transfers == 0:
        return tuple(candidates)

    for member in manager_state.squad.members:
        for incoming in players:
            if incoming.id in owned_ids:
                continue
            if incoming.id not in projection_by_id:
                continue
            candidate = _build_transfer_candidate(manager_state, member, incoming)
            if candidate is not None:
                candidates.append(candidate)

    return tuple(candidates)


def _build_fixed_squad_request(
    candidate: TransferCandidate,
    player_by_id: dict[UUID, Player],
    projection_by_id: dict[UUID, Projection],
    budget: Money,
    target_gameweek: GameweekNumber,
) -> SingleGameweekOptimisationRequest:
    squad_ids = {member.player_id for member in candidate.squad.members}
    assert len(squad_ids) == 15

    ordered_members = sorted(candidate.squad.members, key=lambda m: str(m.player_id))
    fixed_players = tuple(player_by_id[member.player_id] for member in ordered_members)

    for player in fixed_players:
        assert player.id in squad_ids, f"player {player.id} outside candidate squad"

    return SingleGameweekOptimisationRequest(
        target_gameweek=target_gameweek,
        players=fixed_players,
        projections=tuple(projection_by_id[member.player_id] for member in ordered_members),
        budget=budget,
    )

    for player in fixed_players:
        assert player.id in squad_ids, f"player {player.id} outside candidate squad"

    return SingleGameweekOptimisationRequest(
        target_gameweek=target_gameweek,
        players=fixed_players,
        projections=tuple(projection_by_id[member.player_id] for member in ordered_members),
        budget=budget,
    )


def _evaluate_single(
    candidate: TransferCandidate,
    player_by_id: dict[UUID, Player],
    projection_by_id: dict[UUID, Projection],
    optimiser: OptimisationEngine[
        SingleGameweekOptimisationRequest,
        SingleGameweekOptimisationResult,
    ],
    target_gameweek: GameweekNumber,
    roll_score: float,
) -> EvaluatedTransferCandidate:
    squad_cost = Money(
        tenths_million=sum(
            player_by_id[member.player_id].price.tenths_million
            for member in candidate.squad.members
        )
    )
    request = _build_fixed_squad_request(
        candidate,
        player_by_id,
        projection_by_id,
        budget=squad_cost,
        target_gameweek=target_gameweek,
    )
    result = optimiser.optimise(request)
    score = result.primary_objective
    uplift = score - roll_score
    return EvaluatedTransferCandidate(
        candidate=candidate,
        current_gw_score=score,
        uplift_vs_roll=uplift,
        optimisation_result=result,
    )


def _rank_key(
    evaluated: EvaluatedTransferCandidate,
) -> tuple[float, int, tuple[str, str]]:
    return (
        -evaluated.current_gw_score,
        evaluated.candidate.free_transfers_used,
        evaluated.candidate.identity,
    )


def rank_transfer_candidates(
    evaluated: tuple[EvaluatedTransferCandidate, ...],
) -> tuple[EvaluatedTransferCandidate, ...]:
    return tuple(sorted(evaluated, key=_rank_key))


def enumerate_and_rank(
    manager_state: ManagerState,
    players: tuple[Player, ...],
    projections: tuple[Projection, ...],
    optimiser: OptimisationEngine[
        SingleGameweekOptimisationRequest,
        SingleGameweekOptimisationResult,
    ],
) -> tuple[EvaluatedTransferCandidate, ...]:
    player_by_id = {player.id: player for player in players}
    projection_by_id = {proj.player_id: proj for proj in projections}
    target_gameweek = manager_state.gameweek

    candidates = enumerate_transfer_candidates(manager_state, players, projections)

    roll_candidate = candidates[0]
    roll_cost = Money(
        tenths_million=sum(
            player_by_id[member.player_id].price.tenths_million
            for member in roll_candidate.squad.members
        )
    )
    roll_request = _build_fixed_squad_request(
        roll_candidate,
        player_by_id,
        projection_by_id,
        budget=roll_cost,
        target_gameweek=target_gameweek,
    )
    roll_result = optimiser.optimise(roll_request)
    roll_score = roll_result.primary_objective

    evaluated: list[EvaluatedTransferCandidate] = [
        EvaluatedTransferCandidate(
            candidate=roll_candidate,
            current_gw_score=roll_score,
            uplift_vs_roll=0.0,
            optimisation_result=roll_result,
        )
    ]

    for candidate in candidates[1:]:
        evaluated.append(
            _evaluate_single(
                candidate,
                player_by_id,
                projection_by_id,
                optimiser,
                target_gameweek,
                roll_score,
            )
        )

    return rank_transfer_candidates(tuple(evaluated))
