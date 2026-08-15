"""Persist reproducibility metadata for a single-gameweek transfer recommendation."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fpl_decision_engine.domain import (
    DecisionRun,
    DecisionRunStatus,
    ManagerState,
    SingleGameweekTransferOptimisationRequest,
    SingleGameweekTransferOptimisationResult,
)
from fpl_decision_engine.ports import DecisionRunRepository, ProviderResponse


def persist_transfer_decision_run(
    repository: DecisionRunRepository,
    *,
    run_id: UUID,
    created_at: datetime,
    season: str,
    code_revision: str,
    source_is_dirty: bool | None,
    config_fingerprint: str,
    manager_response: ProviderResponse[ManagerState],
    request: SingleGameweekTransferOptimisationRequest,
    result: SingleGameweekTransferOptimisationResult,
    output_artifact_reference: str | None = None,
) -> DecisionRun:
    """Save an explicit DecisionRun row using existing provenance fields only.

    The manager source snapshot identifies exact local bytes; projection lineage comes
    from canonical projections. The recommendation remains a typed in-memory result,
    so no arbitrary model blob is persisted. A caller may reference a separately
    managed explicit artifact when one exists.
    """

    if manager_response.data != request.manager_state:
        raise ValueError("manager provenance response does not match optimisation request")
    snapshot_id = manager_response.provenance.snapshot_id
    input_snapshot_ids = (
        (f"{manager_response.provenance.provider_id}:{snapshot_id}",)
        if snapshot_id is not None
        else ()
    )
    projection_versions = tuple(
        sorted({f"{item.source}:{item.model_version}" for item in request.projections})
    )
    transfer_out_ids = ",".join(str(item.player_out_id) for item in result.transfers) or "none"
    transfer_in_ids = ",".join(str(item.player_in_id) for item in result.transfers) or "none"
    run = DecisionRun(
        id=run_id,
        created_at=created_at,
        season=season,
        gameweek=request.target_gameweek,
        code_revision=code_revision,
        source_is_dirty=source_is_dirty,
        config_fingerprint=config_fingerprint,
        input_snapshot_ids=input_snapshot_ids,
        projection_versions=projection_versions,
        optimiser_engine="highs-single-gameweek-transfers-v1",
        optimiser_version=result.solver_name,
        optimiser_settings=(
            ("free_transfers_remaining", str(request.manager_state.free_transfers)),
            ("hit_cost", "4"),
            ("max_transfers", str(request.max_transfers)),
        ),
        strategy_mode="normal_single_gameweek_transfers",
        objective_mode="mean_only_net_incremental_hits",
        random_seed=0,
        output_artifact_references=(
            (output_artifact_reference,) if output_artifact_reference is not None else ()
        ),
        status=DecisionRunStatus.SUCCEEDED,
        diagnostic_summary=(
            f"status={result.solver_status}; transfer_out_ids={transfer_out_ids}; "
            f"transfer_in_ids={transfer_in_ids}; transfer_count={result.transfer_count}; "
            f"incremental_hit={result.additional_points_cost}; "
            f"expected_gain={result.expected_gain:.6f}; "
            f"bank_after_tenths_million={result.bank_after.tenths_million}"
        ),
    )
    repository.save(run)
    return run
