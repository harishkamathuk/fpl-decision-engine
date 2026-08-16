"""Application orchestration for multi-gameweek planning and provenance."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from fpl_decision_engine.domain import (
    DecisionRun,
    DecisionRunStatus,
    ManagerState,
    MultiGameweekPlanningRequest,
    MultiGameweekPlanningResult,
)
from fpl_decision_engine.ports import (
    DecisionRunRepository,
    OptimisationEngine,
    ProviderResponse,
)


def compare_planning_horizons(
    engine: OptimisationEngine[
        MultiGameweekPlanningRequest,
        MultiGameweekPlanningResult,
    ],
    request: MultiGameweekPlanningRequest,
    horizons: Iterable[int],
) -> tuple[tuple[int, MultiGameweekPlanningResult], ...]:
    """Evaluate explicit horizon variants without hiding extra solves in the engine."""

    comparisons: list[tuple[int, MultiGameweekPlanningResult]] = []
    for horizon in horizons:
        if horizon < 1 or horizon > request.horizon:
            raise ValueError("comparison horizon must be within the supplied planning horizon")
        final_gameweek = request.starting_gameweek.value + horizon - 1
        projections = tuple(
            projection
            for projection in request.projections
            if request.starting_gameweek.value <= projection.gameweek.value <= final_gameweek
        )
        variant = request.model_copy(
            update={
                "horizon": horizon,
                "projections": projections,
                "gameweek_weights": (
                    request.gameweek_weights[:horizon] if request.gameweek_weights else ()
                ),
                "max_transfers_per_gameweek": (
                    request.max_transfers_per_gameweek[:horizon]
                    if request.max_transfers_per_gameweek
                    else ()
                ),
            }
        )
        comparisons.append((horizon, engine.optimise(variant)))
    return tuple(comparisons)


def persist_planning_decision_run(
    repository: DecisionRunRepository,
    *,
    run_id: UUID,
    created_at: datetime,
    season: str,
    code_revision: str,
    source_is_dirty: bool | None,
    config_fingerprint: str,
    manager_response: ProviderResponse[ManagerState],
    request: MultiGameweekPlanningRequest,
    result: MultiGameweekPlanningResult,
    output_artifact_reference: str | None = None,
) -> DecisionRun:
    """Persist planning provenance and the first actionable recommendation summary.

    The complete trajectory remains a typed result. The existing DecisionRun row
    records deterministic inputs, strategy settings, first-Gameweek action and
    aggregate gain without serialising an arbitrary domain-model blob.
    """

    if manager_response.data != request.initial_manager_state:
        raise ValueError("manager provenance response does not match planning request")
    if result.horizon != request.horizon:
        raise ValueError("planning result horizon does not match request")
    snapshot_id = manager_response.provenance.snapshot_id
    input_snapshot_ids = (
        (f"{manager_response.provenance.provider_id}:{snapshot_id}",)
        if snapshot_id is not None
        else ()
    )
    projection_versions = tuple(
        sorted({f"{item.source}:{item.model_version}" for item in request.projections})
    )
    actionable = result.actionable_gameweek
    transfer_out_ids = ",".join(map(str, actionable.transfer_out_ids)) or "none"
    transfer_in_ids = ",".join(map(str, actionable.transfer_in_ids)) or "none"
    target_gameweeks = ",".join(str(item.value) for item in request.target_gameweeks)
    weights = ",".join(f"{item:.12g}" for item in request.resolved_weights)
    transfer_limits = ",".join(map(str, request.resolved_transfer_limits))
    run = DecisionRun(
        id=run_id,
        created_at=created_at,
        season=season,
        gameweek=request.starting_gameweek,
        code_revision=code_revision,
        source_is_dirty=source_is_dirty,
        config_fingerprint=config_fingerprint,
        input_snapshot_ids=input_snapshot_ids,
        projection_versions=projection_versions,
        optimiser_engine="highs-multi-gameweek-planner-v1",
        optimiser_version=result.solver_name,
        optimiser_settings=(
            ("discount_factor", f"{request.discount_factor:.12g}"),
            ("gameweek_weights", weights),
            ("hit_cost", "4"),
            ("horizon", str(request.horizon)),
            ("max_transfers_per_gameweek", transfer_limits),
            ("price_policy", "decision_time_frozen"),
            ("target_gameweeks", target_gameweeks),
        ),
        strategy_mode="receding_horizon_normal_transfers",
        objective_mode="weighted_mean_only_net_incremental_hits",
        random_seed=0,
        output_artifact_references=(
            (output_artifact_reference,) if output_artifact_reference is not None else ()
        ),
        status=DecisionRunStatus.SUCCEEDED,
        diagnostic_summary=(
            f"status={result.solver_status}; first_transfer_out_ids={transfer_out_ids}; "
            f"first_transfer_in_ids={transfer_in_ids}; "
            f"first_transfer_count={actionable.transfer_count}; "
            f"first_incremental_hit={actionable.hit_cost}; "
            f"horizon={result.horizon}; "
            f"weighted_gain={result.weighted_expected_gain:.6f}"
        ),
    )
    repository.save(run)
    return run
