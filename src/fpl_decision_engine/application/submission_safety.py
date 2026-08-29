"""Bounded safety decisions for planning and verifying FPL submissions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fpl_decision_engine.domain.decision_bundle import DecisionBundleV1, SubmittedDecision
from fpl_decision_engine.domain.manager_state import (
    ManagerComparison,
    ManagerStateFailure,
    ManagerStateResult,
    ManagerStateSnapshot,
    ManagerVerification,
)


class SafetyStatus(StrEnum):
    """Deterministic outcome of one safety phase."""

    SAFE = "SAFE"
    BLOCKED = "BLOCKED"
    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    UNVERIFIED = "UNVERIFIED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class PreviousReconciliation(StrEnum):
    """Relationship between an explicitly supplied previous actual state and current state."""

    NOT_SUPPLIED = "NOT_SUPPLIED"
    MATCHED = "MATCHED"
    DIFFERENT = "DIFFERENT"


@dataclass(frozen=True, slots=True)
class SubmissionSafetyResult:
    """Immutable result containing only the evidence needed by Issue #88."""

    phase: str
    status: SafetyStatus
    blocking: bool
    details: tuple[str, ...] = ()
    manager_state_identity: str | None = None
    decision_run_id: UUID | None = None
    decision_identity: str | None = None
    previous_reconciliation: PreviousReconciliation = PreviousReconciliation.NOT_SUPPLIED
    acknowledgement_required: bool = False
    acknowledged: bool = False
    actual_choice: SubmittedDecision | None = None


def plan_submission(
    current: ManagerStateResult,
    decision: DecisionBundleV1 | None,
    *,
    expected_entry_id: int,
    expected_gameweek: int,
    previous_verified: ManagerStateSnapshot | None = None,
    previous_acknowledged: bool = False,
    finance_required: bool = False,
    player_element_ids: Mapping[UUID, int] | None = None,
) -> SubmissionSafetyResult:
    """Apply the pre-execution predicate without inferring history or finance values."""
    identity = current.snapshot.semantic_identity if current.snapshot else None
    previous = _reconcile_previous(previous_verified, current.snapshot)
    if previous is PreviousReconciliation.DIFFERENT and not previous_acknowledged:
        acknowledgement_required = True
    else:
        acknowledgement_required = previous is PreviousReconciliation.DIFFERENT
    blocked = _state_block(current)
    details = list(_state_details(current))
    if current.snapshot is not None:
        if current.snapshot.manager_entry_id != expected_entry_id:
            blocked = True
            details.append("manager identity mismatch")
        if current.snapshot.target_event_id.value != expected_gameweek:
            blocked = True
            details.append("target Gameweek mismatch")
        if finance_required and any(
            value is None
            for value in (current.snapshot.free_transfers, current.snapshot.selling_prices)
        ):
            blocked = True
            details.append("required finance/transfer state is unavailable")
    if decision is None:
        blocked = True
        details.append("FINAL_DECISION is missing")
    elif decision.gameweek.value != expected_gameweek:
        blocked = True
        details.append("FINAL_DECISION Gameweek mismatch")
    if acknowledgement_required and not previous_acknowledged:
        blocked = True
        details.append("previous-state reconciliation acknowledgement is required")
    if decision is not None and current.snapshot is not None and not blocked:
        try:
            _snapshot_from_decision(decision, current.snapshot, player_element_ids)
        except ValueError as exc:
            blocked = True
            details.append(str(exc))
    if blocked:
        return SubmissionSafetyResult(
            phase="PRE_EXECUTION",
            status=SafetyStatus.BLOCKED,
            blocking=True,
            details=tuple(details),
            manager_state_identity=identity,
            decision_run_id=decision.decision_run_id if decision else None,
            decision_identity=_decision_identity(decision),
            previous_reconciliation=previous,
            acknowledgement_required=acknowledgement_required,
            acknowledged=previous_acknowledged,
        )
    assert decision is not None
    return SubmissionSafetyResult(
        phase="PRE_EXECUTION",
        status=SafetyStatus.SAFE,
        blocking=False,
        details=tuple(details),
        manager_state_identity=identity,
        decision_run_id=decision.decision_run_id,
        decision_identity=_decision_identity(decision),
        previous_reconciliation=previous,
        acknowledgement_required=acknowledgement_required,
        acknowledged=previous_acknowledged,
    )


def verify_submission(
    final_decision: DecisionBundleV1,
    post_execution: ManagerStateResult,
    *,
    player_element_ids: Mapping[UUID, int] | None = None,
    element_player_ids: Mapping[int, UUID] | None = None,
) -> SubmissionSafetyResult:
    """Permit verification only for a fresh, verified observation equal to FINAL_DECISION."""
    identity = post_execution.snapshot.semantic_identity if post_execution.snapshot else None
    if post_execution.failure is ManagerStateFailure.SOURCE_UNAVAILABLE:
        status = SafetyStatus.SOURCE_UNAVAILABLE
        details = ("fresh post-execution manager state is unavailable",)
        blocking = True
    elif post_execution.verification is not ManagerVerification.VERIFIED:
        status = SafetyStatus.UNVERIFIED
        details = ("fresh post-execution manager state is unverified",)
        blocking = True
    else:
        assert post_execution.snapshot is not None
        try:
            expected = _snapshot_from_decision(
                final_decision, post_execution.snapshot, player_element_ids
            )
        except ValueError as exc:
            return SubmissionSafetyResult(
                phase="POST_EXECUTION",
                status=SafetyStatus.MISMATCHED,
                blocking=True,
                details=(str(exc),),
                manager_state_identity=identity,
                decision_run_id=final_decision.decision_run_id,
                decision_identity=_decision_identity(final_decision),
            )
        comparison, differences = _compare_submission_state(post_execution.snapshot, expected)
        status = (
            SafetyStatus.MATCHED
            if comparison is ManagerComparison.MATCHED
            else SafetyStatus.MISMATCHED
        )
        details = differences
        blocking = status is not SafetyStatus.MATCHED
        try:
            if element_player_ids is None:
                raise ValueError("verified FPL state to player mapping is missing")
            actual = _actual_choice(post_execution.snapshot, element_player_ids)
        except ValueError as exc:
            return SubmissionSafetyResult(
                phase="POST_EXECUTION",
                status=SafetyStatus.MISMATCHED,
                blocking=True,
                details=(str(exc),),
                manager_state_identity=identity,
                decision_run_id=final_decision.decision_run_id,
                decision_identity=_decision_identity(final_decision),
            )
        return SubmissionSafetyResult(
            phase="POST_EXECUTION",
            status=status,
            blocking=blocking,
            details=details,
            manager_state_identity=identity,
            decision_run_id=final_decision.decision_run_id,
            decision_identity=_decision_identity(final_decision),
            actual_choice=actual,
        )
    return SubmissionSafetyResult(
        phase="POST_EXECUTION",
        status=status,
        blocking=blocking,
        details=details,
        manager_state_identity=identity,
        decision_run_id=final_decision.decision_run_id,
        decision_identity=_decision_identity(final_decision),
    )


def _state_block(result: ManagerStateResult) -> bool:
    return result.verification is not ManagerVerification.VERIFIED


def _state_details(result: ManagerStateResult) -> tuple[str, ...]:
    if result.failure is None:
        return ()
    return (result.failure.value,)


def _reconcile_previous(
    previous: ManagerStateSnapshot | None, current: ManagerStateSnapshot | None
) -> PreviousReconciliation:
    if previous is None:
        return PreviousReconciliation.NOT_SUPPLIED
    if current is None:
        return PreviousReconciliation.DIFFERENT
    comparison, _ = _compare_submission_state(current, previous)
    return (
        PreviousReconciliation.MATCHED
        if comparison is ManagerComparison.MATCHED
        else PreviousReconciliation.DIFFERENT
    )


def _compare_submission_state(
    actual: ManagerStateSnapshot, expected: ManagerStateSnapshot
) -> tuple[ManagerComparison, tuple[str, ...]]:
    """Compare set-valued squad/XI fields without weakening ordered bench semantics."""
    fields = (
        "manager_entry_id",
        "target_event_id",
        "captain_player_id",
        "vice_captain_player_id",
        "reserve_goalkeeper_player_id",
        "ordered_outfield_bench_player_ids",
    )
    differences = [field for field in fields if getattr(actual, field) != getattr(expected, field)]
    if set(actual.squad_player_ids) != set(expected.squad_player_ids):
        differences.append("squad_player_ids")
    if set(actual.starting_xi_player_ids) != set(expected.starting_xi_player_ids):
        differences.append("starting_xi_player_ids")
    return (
        (ManagerComparison.MISMATCHED, tuple(differences))
        if differences
        else (ManagerComparison.MATCHED, ())
    )


def _decision_identity(decision: DecisionBundleV1 | None) -> str | None:
    if decision is None:
        return None
    return repr(decision.recommendation.identity)


def _snapshot_from_decision(
    decision: DecisionBundleV1,
    observed: ManagerStateSnapshot,
    player_element_ids: Mapping[UUID, int] | None,
) -> ManagerStateSnapshot:
    """Adapt internal UUID selections through an explicit deterministic adapter."""
    mapping = _selection_mapping(decision, observed, player_element_ids)
    recommendation = decision.recommendation
    return observed.model_copy(
        update={
            "squad_player_ids": tuple(sorted(mapping[value] for value in recommendation.squad_ids)),
            "starting_xi_player_ids": tuple(
                sorted(mapping[value] for value in recommendation.starting_xi_ids)
            ),
            "captain_player_id": mapping[recommendation.captain_id],
            "vice_captain_player_id": mapping[recommendation.vice_captain_id],
            "reserve_goalkeeper_player_id": mapping[recommendation.bench_ids[0]],
            "ordered_outfield_bench_player_ids": tuple(
                mapping[value] for value in recommendation.bench_ids[1:]
            ),
        }
    )


def _selection_mapping(
    decision: DecisionBundleV1,
    observed: ManagerStateSnapshot,
    player_element_ids: Mapping[UUID, int] | None,
) -> dict[UUID, int]:
    """Validate the caller-owned canonical-player to FPL-element adapter."""
    ids = tuple(
        dict.fromkeys(
            (
                *decision.recommendation.squad_ids,
                *decision.recommendation.starting_xi_ids,
                *decision.recommendation.bench_ids,
                decision.recommendation.captain_id,
                decision.recommendation.vice_captain_id,
            )
        )
    )
    if player_element_ids is None:
        raise ValueError("FINAL_DECISION to FPL-ID mapping is missing")
    missing = tuple(value for value in ids if value not in player_element_ids)
    if missing:
        raise ValueError("FINAL_DECISION to FPL-ID mapping is incomplete")
    mapping = {value: player_element_ids[value] for value in ids}
    if any(value <= 0 for value in mapping.values()):
        raise ValueError("FINAL_DECISION to FPL-ID mapping contains invalid element IDs")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("FINAL_DECISION to FPL-ID mapping is ambiguous")
    if set(mapping.values()) != set(observed.squad_player_ids):
        raise ValueError("FINAL_DECISION to FPL-ID mapping does not cover observed squad")
    return mapping


def _actual_choice(
    snapshot: ManagerStateSnapshot,
    element_player_ids: Mapping[int, UUID],
) -> SubmittedDecision:
    """Adapt verified FPL element IDs back through an explicit identity seam."""
    ids = tuple(
        (
            *snapshot.squad_player_ids,
            *snapshot.starting_xi_player_ids,
            snapshot.captain_player_id,
            snapshot.vice_captain_player_id,
            snapshot.reserve_goalkeeper_player_id,
            *snapshot.ordered_outfield_bench_player_ids,
        )
    )
    if any(value not in element_player_ids for value in ids):
        raise ValueError("verified FPL state to player mapping is incomplete")
    return SubmittedDecision(
        squad_ids=tuple(
            sorted(
                (element_player_ids[value] for value in snapshot.squad_player_ids),
                key=str,
            )
        ),
        starting_xi_ids=tuple(
            sorted(
                (element_player_ids[value] for value in snapshot.starting_xi_player_ids),
                key=str,
            )
        ),
        captain_id=element_player_ids[snapshot.captain_player_id],
        vice_captain_id=element_player_ids[snapshot.vice_captain_player_id],
        bench_ids=tuple(
            element_player_ids[value]
            for value in (
                snapshot.reserve_goalkeeper_player_id,
                *snapshot.ordered_outfield_bench_player_ids,
            )
        ),
        recorded_at=snapshot.acquired_at_utc,
    )
