"""Bounded safety decisions for planning and verifying FPL submissions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import UUID

from fpl_decision_engine.application.decision_bundles import serialize_decision_bundle
from fpl_decision_engine.domain.decision_bundle import DecisionBundleV1, SubmittedDecision
from fpl_decision_engine.domain.manager_state import (
    ManagerComparison,
    ManagerStateFailure,
    ManagerStateResult,
    ManagerStateSnapshot,
    ManagerVerification,
)

SUBMISSION_SAFETY_ARTEFACT_KIND_V1 = "submission-safety-result-v1"
SUBMISSION_SAFETY_ARTEFACT_KIND = "submission-safety-result-v2"
SUBMISSION_SAFETY_SCHEMA_VERSION = 2
_SUBMISSION_SAFETY_KINDS = {
    1: SUBMISSION_SAFETY_ARTEFACT_KIND_V1,
    SUBMISSION_SAFETY_SCHEMA_VERSION: SUBMISSION_SAFETY_ARTEFACT_KIND,
}


class SubmissionSafetyArtifactError(RuntimeError):
    """Raised when persisted submission-safety evidence cannot be replayed safely."""


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


@dataclass(frozen=True, slots=True)
class SubmissionSafetyArtifact:
    """Immutable content-addressed reference to one persisted safety result."""

    path: Path
    reference: str
    sha256: str


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


def serialize_submission_safety_result(result: SubmissionSafetyResult) -> bytes:
    """Serialize a safety result canonically for immutable provenance recording."""
    payload = {
        "schema_version": SUBMISSION_SAFETY_SCHEMA_VERSION,
        "kind": SUBMISSION_SAFETY_ARTEFACT_KIND,
        "phase": result.phase,
        "status": result.status.value,
        "blocking": result.blocking,
        "details": result.details,
        "manager_state_identity": result.manager_state_identity,
        "decision_run_id": str(result.decision_run_id) if result.decision_run_id else None,
        "decision_identity": result.decision_identity,
        "previous_reconciliation": result.previous_reconciliation.value,
        "acknowledgement_required": result.acknowledgement_required,
        "acknowledged": result.acknowledged,
        "actual_choice": (
            result.actual_choice.model_dump(mode="json")
            if result.actual_choice is not None
            else None
        ),
    }
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (content + "\n").encode()


def write_submission_safety_result(
    result: SubmissionSafetyResult, *, state_root: Path
) -> SubmissionSafetyArtifact:
    """Publish an immutable content-addressed submission-safety artefact."""
    content = serialize_submission_safety_result(result)
    digest = hashlib.sha256(content).hexdigest()
    directory = state_root / "submission-safety"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(
                    "submission-safety artefact hash path contains conflicting bytes"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return SubmissionSafetyArtifact(path=path, reference=str(path), sha256=digest)


def load_submission_safety_result(
    *,
    reference: str,
    sha256: str,
    expected_phase: str | None = None,
    expected_decision: DecisionBundleV1 | None = None,
) -> SubmissionSafetyResult:
    """Read, hash-check and reconstruct one persisted safety result.

    Current v2 evidence binds to the SHA-256 of the exact canonical DecisionBundle
    bytes. Historical v1 evidence remains readable, but cannot be reused as proof for
    an expected decision because its decision identity was an opaque Python repr.
    """
    try:
        content = Path(reference).read_bytes()
    except OSError as exc:
        raise SubmissionSafetyArtifactError(
            f"cannot read submission-safety artefact {reference!r}: {exc}"
        ) from exc
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != sha256:
        raise SubmissionSafetyArtifactError(
            "submission-safety artefact SHA-256 mismatch: "
            f"claimed {sha256}, computed {actual_sha256}"
        )
    try:
        raw_payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SubmissionSafetyArtifactError(
            f"submission-safety artefact is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw_payload, dict):
        raise SubmissionSafetyArtifactError("submission-safety artefact must be a JSON object")
    payload = cast(dict[str, object], raw_payload)
    schema_version = payload.get("schema_version")
    if schema_version not in _SUBMISSION_SAFETY_KINDS:
        raise SubmissionSafetyArtifactError(
            "unsupported submission-safety artefact schema_version "
            f"{schema_version!r}"
        )
    assert isinstance(schema_version, int)
    expected_kind = _SUBMISSION_SAFETY_KINDS[schema_version]
    if payload.get("kind") != expected_kind:
        raise SubmissionSafetyArtifactError(
            f"unexpected submission-safety artefact kind {payload.get('kind')!r}"
        )
    result = _result_from_payload(payload)
    if expected_phase is not None and result.phase != expected_phase:
        raise SubmissionSafetyArtifactError(
            f"submission-safety artefact phase {result.phase!r} does not match "
            f"expected {expected_phase!r}"
        )
    _validate_result_consistency(result)
    _validate_decision_binding(
        result, schema_version=schema_version, expected_decision=expected_decision
    )
    return result


def _result_from_payload(payload: dict[str, object]) -> SubmissionSafetyResult:
    try:
        phase = _required_str(payload, "phase")
        status = SafetyStatus(_required_str(payload, "status"))
        blocking = _required_bool(payload, "blocking")
        actual_payload = payload.get("actual_choice")
        actual_choice = (
            SubmittedDecision.model_validate(actual_payload)
            if actual_payload is not None
            else None
        )
        decision_run_id_value = _optional_str(payload, "decision_run_id")
        raw_details = payload.get("details", ())
        if not isinstance(raw_details, list | tuple):
            raise ValueError("details must be a list of strings")
        details: list[str] = []
        for item in cast(Sequence[object], raw_details):
            if not isinstance(item, str):
                raise ValueError("details must be a list of strings")
            details.append(item)
        return SubmissionSafetyResult(
            phase=phase,
            status=status,
            blocking=blocking,
            details=tuple(details),
            manager_state_identity=_optional_str(payload, "manager_state_identity"),
            decision_run_id=UUID(decision_run_id_value)
            if decision_run_id_value is not None
            else None,
            decision_identity=_optional_str(payload, "decision_identity"),
            previous_reconciliation=PreviousReconciliation(
                _optional_str(payload, "previous_reconciliation")
                or PreviousReconciliation.NOT_SUPPLIED.value
            ),
            acknowledgement_required=_optional_bool(
                payload, "acknowledgement_required", default=False
            ),
            acknowledged=_optional_bool(payload, "acknowledged", default=False),
            actual_choice=actual_choice,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SubmissionSafetyArtifactError(
            f"invalid submission-safety artefact payload: {exc}"
        ) from exc


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _optional_bool(payload: dict[str, object], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _validate_result_consistency(result: SubmissionSafetyResult) -> None:
    if result.phase == "PRE_EXECUTION":
        if result.status not in (SafetyStatus.SAFE, SafetyStatus.BLOCKED):
            raise SubmissionSafetyArtifactError(
                "PRE_EXECUTION safety artefact has invalid status "
                f"{result.status.value!r}"
            )
        if result.actual_choice is not None:
            raise SubmissionSafetyArtifactError(
                "PRE_EXECUTION safety artefact must not contain ACTUAL_CHOICE"
            )
    elif result.phase == "POST_EXECUTION":
        if result.status not in (
            SafetyStatus.MATCHED,
            SafetyStatus.MISMATCHED,
            SafetyStatus.UNVERIFIED,
            SafetyStatus.SOURCE_UNAVAILABLE,
        ):
            raise SubmissionSafetyArtifactError(
                "POST_EXECUTION safety artefact has invalid status "
                f"{result.status.value!r}"
            )
        if result.status is SafetyStatus.MATCHED and result.actual_choice is None:
            raise SubmissionSafetyArtifactError(
                "MATCHED POST_EXECUTION safety artefact requires ACTUAL_CHOICE"
            )
    else:
        raise SubmissionSafetyArtifactError(
            f"unsupported submission-safety phase {result.phase!r}"
        )
    expected_blocking = result.status not in (SafetyStatus.SAFE, SafetyStatus.MATCHED)
    if result.blocking is not expected_blocking:
        raise SubmissionSafetyArtifactError(
            f"submission-safety blocking flag conflicts with status {result.status.value}"
        )


def _validate_decision_binding(
    result: SubmissionSafetyResult,
    *,
    schema_version: int,
    expected_decision: DecisionBundleV1 | None,
) -> None:
    if schema_version == 1:
        if expected_decision is not None:
            raise SubmissionSafetyArtifactError(
                "submission-safety schema_version 1 cannot prove canonical decision identity"
            )
        return
    identity = result.decision_identity
    if identity is not None and (
        len(identity) != 64
        or any(character not in "0123456789abcdef" for character in identity)
    ):
        raise SubmissionSafetyArtifactError(
            "submission-safety decision_identity must be a lowercase SHA-256 digest"
        )
    if (result.decision_run_id is None) != (identity is None):
        raise SubmissionSafetyArtifactError(
            "submission-safety decision_run_id and decision_identity must be recorded together"
        )
    if expected_decision is None:
        return
    if result.decision_run_id != expected_decision.decision_run_id:
        raise SubmissionSafetyArtifactError(
            "submission-safety decision_run_id does not match the expected DecisionBundle"
        )
    expected_identity = _decision_identity(expected_decision)
    if identity != expected_identity:
        raise SubmissionSafetyArtifactError(
            "submission-safety decision_identity does not match the expected DecisionBundle"
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
    return hashlib.sha256(serialize_decision_bundle(decision)).hexdigest()


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
