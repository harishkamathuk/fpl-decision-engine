"""Persist reproducibility metadata for a blank-squad #6 recommendation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from fpl_decision_engine.domain import (
    DecisionRun,
    DecisionRunStatus,
    SingleGameweekOptimisationRequest,
    SingleGameweekOptimisationResult,
)
from fpl_decision_engine.ports import DecisionRunRepository


def _uuid_list(values: Iterable[UUID]) -> str:
    return ",".join(str(value) for value in values)


def persist_squad_decision_run(
    repository: DecisionRunRepository,
    *,
    run_id: UUID,
    created_at: datetime,
    season: str,
    code_revision: str,
    source_is_dirty: bool | None,
    config_fingerprint: str,
    input_snapshot_references: tuple[str, ...],
    request: SingleGameweekOptimisationRequest,
    result: SingleGameweekOptimisationResult,
    output_artifact_reference: str | None = None,
) -> DecisionRun:
    """Persist exact #6 inputs and deterministic recommendation identity.

    Snapshot references are supplied by the orchestration caller because canonical
    players and projections deliberately do not carry file-level source envelopes.
    The solver remains infrastructure-only and unaware of persistence.
    """

    candidate_ids = {player.id for player in request.players}
    recommendation_ids = {member.player_id for member in result.squad.members}
    if not recommendation_ids <= candidate_ids:
        raise ValueError("recommendation contains a player outside the optimisation request")
    snapshot_references = tuple(sorted(set(input_snapshot_references)))
    projection_versions = tuple(
        sorted({f"{item.source}:{item.model_version}" for item in request.projections})
    )
    squad_ids = tuple(sorted(recommendation_ids, key=str))
    starting_xi_ids = tuple(sorted(result.starting_xi, key=str))
    if request.captain_fallback:
        optimiser_engine = "highs-single-gameweek-v2"
        objective_mode = "xi_plus_captain_with_vice_fallback"
    else:
        optimiser_engine = "highs-single-gameweek-optimiser-v1"
        objective_mode = "mean_only_xi_plus_captain"
    settings = (
        ("budget_tenths_million", str(request.budget.tenths_million)),
        ("captain_fallback", str(request.captain_fallback).lower()),
        ("excluded_player_ids", _uuid_list(sorted(request.excluded_players, key=str)) or "none"),
        (
            "forced_captain_id",
            str(request.forced_captain) if request.forced_captain is not None else "none",
        ),
        (
            "forced_starter_ids",
            _uuid_list(sorted(request.forced_starters, key=str)) or "none",
        ),
        (
            "forced_vice_captain_id",
            str(request.forced_vice_captain) if request.forced_vice_captain is not None else "none",
        ),
        (
            "must_include_player_ids",
            _uuid_list(sorted(request.must_include_in_squad, key=str)) or "none",
        ),
    )
    run = DecisionRun(
        id=run_id,
        created_at=created_at,
        season=season,
        gameweek=request.target_gameweek,
        code_revision=code_revision,
        source_is_dirty=source_is_dirty,
        config_fingerprint=config_fingerprint,
        input_snapshot_ids=snapshot_references,
        projection_versions=projection_versions,
        optimiser_engine=optimiser_engine,
        optimiser_version=result.solver_name,
        optimiser_settings=settings,
        strategy_mode="blank_squad_single_gameweek",
        objective_mode=objective_mode,
        random_seed=0,
        output_artifact_references=(
            (output_artifact_reference,) if output_artifact_reference is not None else ()
        ),
        status=DecisionRunStatus.SUCCEEDED,
        diagnostic_summary=(
            f"status={result.solver_status}; squad_ids={_uuid_list(squad_ids)}; "
            f"starting_xi_ids={_uuid_list(starting_xi_ids)}; "
            f"captain_id={result.captain_id}; vice_captain_id={result.vice_captain_id}; "
            f"bench_ids={_uuid_list(result.bench)}; formation={result.formation.label}; "
            f"squad_cost_tenths_million={result.squad_cost.tenths_million}; "
            f"bank_remaining_tenths_million={result.bank_remaining.tenths_million}; "
            f"primary_objective={result.primary_objective:.6f}; "
            f"solver_status={result.solver_status}"
        ),
    )
    repository.save(run)
    return run
