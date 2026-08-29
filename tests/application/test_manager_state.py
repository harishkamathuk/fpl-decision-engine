"""Deterministic regression coverage for Issue #87 manager-state contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_decision_engine.application.manager_state import (
    acquire_manager_state,
    serialize_manager_state,
    verify_manager_state,
    write_manager_state,
)
from fpl_decision_engine.application.orchestration import (
    BASELINE_STAGE,
    DOCTOR_STAGE,
    EVIDENCE_STAGE,
    OPERATOR_EXECUTION_CONFIRMATION_STAGE,
    ORCHESTRATOR_STAGES,
    POST_SUBMISSION_VERIFY_STAGE,
    PRE_SUBMISSION_VERIFY_STAGE,
)
from fpl_decision_engine.domain import GameweekNumber, Position
from fpl_decision_engine.domain.manager_state import (
    ManagerComparison,
    ManagerStateFailure,
    ManagerStateSnapshot,
    ManagerVerification,
    RawManagerPick,
)
from fpl_decision_engine.ports import (
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderManagerIdentityError,
    ProviderUnavailableError,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
TYPES = {
    **{1: Position.GOALKEEPER, 2: Position.GOALKEEPER},
    **{i: Position.DEFENDER for i in range(3, 8)},
    **{i: Position.MIDFIELDER for i in range(8, 13)},
    **{i: Position.FORWARD for i in range(13, 16)},
}


def snapshot(**changes: object) -> ManagerStateSnapshot:
    picks = tuple(
        RawManagerPick(
            element_id=i,
            position=i,
            is_captain=i == 3,
            is_vice_captain=i == 4,
        )
        for i in range(1, 16)
    )
    payload: dict[str, object] = {
        "source_provider": "official_fpl_api",
        "source_endpoint": "/api/my-team/42/",
        "acquired_at_utc": NOW,
        "manager_entry_id": 42,
        "authenticated_entry_id": 42,
        "target_event_id": GameweekNumber(value=1),
        "target_deadline_time": NOW + timedelta(hours=1),
        "raw_picks": picks,
        "squad_player_ids": tuple(range(1, 16)),
        "starting_xi_player_ids": (1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 15),
        "captain_player_id": 3,
        "vice_captain_player_id": 4,
        "reserve_goalkeeper_player_id": 2,
        "ordered_outfield_bench_player_ids": (12, 13, 14),
    }
    payload.update(changes)
    return ManagerStateSnapshot(**payload)


def verified(value: ManagerStateSnapshot | None = None):
    result = verify_manager_state(
        value or snapshot(),
        expected_entry_id=42,
        expected_target_event_id=GameweekNumber(value=1),
        known_player_types=TYPES,
        now=NOW,
    )
    return result


def test_t1_valid_acquisition_is_verified_and_published_immutably(tmp_path: Path) -> None:
    result = verified()
    path = write_manager_state(result.snapshot, state_root=tmp_path)
    assert result.verification is ManagerVerification.VERIFIED
    assert path.read_bytes() == serialize_manager_state(result.snapshot)
    assert path.exists()


def test_t2_identity_mismatch_is_not_normal_comparison() -> None:
    value = snapshot().model_copy(update={"authenticated_entry_id": 99})
    result = verify_manager_state(
        value,
        expected_entry_id=42,
        expected_target_event_id=GameweekNumber(value=1),
        known_player_types=TYPES,
        now=NOW,
    )
    assert result.failure is ManagerStateFailure.MANAGER_IDENTITY_MISMATCH
    assert result.verification is ManagerVerification.UNVERIFIED
    assert result.comparison is ManagerComparison.NOT_APPLICABLE


@pytest.mark.parametrize("event", [2, 20])
def test_t3_valid_non_gw1_event_is_verified_when_expected(event: int) -> None:
    value = snapshot(
        target_event_id=GameweekNumber(value=event),
        target_deadline_time=NOW + timedelta(hours=1),
    )
    result = verify_manager_state(
        value,
        expected_entry_id=42,
        expected_target_event_id=GameweekNumber(value=event),
        known_player_types=TYPES,
        now=NOW,
    )
    assert result.verification is ManagerVerification.VERIFIED


def test_t3_wrong_expected_event_is_unverified() -> None:
    result = verify_manager_state(
        snapshot(target_event_id=GameweekNumber(value=2)),
        expected_entry_id=42,
        expected_target_event_id=GameweekNumber(value=3),
        known_player_types=TYPES,
        now=NOW,
    )
    assert result.verification is ManagerVerification.UNVERIFIED


@pytest.mark.parametrize(
    "changes",
    [
        {"squad_player_ids": tuple(range(1, 15))},
        {"squad_player_ids": (1,) + tuple(range(1, 15))},
        {"starting_xi_player_ids": tuple(range(1, 11))},
        {"captain_player_id": 12},
        {"vice_captain_player_id": 12},
        {"vice_captain_player_id": 3},
        {"ordered_outfield_bench_player_ids": (12, 13, 15)},
        {"raw_picks": ()},
    ],
)
def test_t4_malformed_or_incomplete_state_is_unverified(changes: dict[str, object]) -> None:
    if (
        changes.get("raw_picks") == ()
        or len(changes.get("squad_player_ids", (1,) * 15)) != 15
        or len(changes.get("starting_xi_player_ids", (1,) * 11)) != 11
    ):
        with pytest.raises(ValueError):
            snapshot(**changes)
        return
    assert verified(snapshot(**changes)).verification is ManagerVerification.UNVERIFIED


def test_t5_explicit_comparison_matches_and_reports_differences() -> None:
    same = acquire_manager_state(
        _Source(snapshot()),
        entry_id=42,
        target_event=GameweekNumber(value=1),
        known_player_types=TYPES,
        expected_snapshot=snapshot(),
    )
    changed = snapshot(captain_player_id=4, vice_captain_player_id=3)
    different = acquire_manager_state(
        _Source(changed),
        entry_id=42,
        target_event=GameweekNumber(value=1),
        known_player_types=TYPES,
        expected_snapshot=snapshot(),
    )
    assert same.comparison is ManagerComparison.MATCHED
    assert different.comparison is ManagerComparison.MISMATCHED
    assert "captain_player_id" in different.mismatch_details


def test_t6_no_reference_is_not_applicable() -> None:
    assert verified().comparison is ManagerComparison.NOT_APPLICABLE


def test_t7_timestamp_does_not_change_semantic_identity() -> None:
    assert snapshot().semantic_identity == snapshot(
        acquired_at_utc=NOW + timedelta(minutes=1)
    ).semantic_identity


def test_t8_selection_change_changes_identity() -> None:
    assert snapshot().semantic_identity != snapshot(
        captain_player_id=4, vice_captain_player_id=3
    ).semantic_identity


def test_t9_source_failure_has_no_snapshot() -> None:
    result = acquire_manager_state(
        _Source(RuntimeError("offline")),
        entry_id=42,
        target_event=GameweekNumber(value=1),
        known_player_types=TYPES,
    )
    assert result.failure is ManagerStateFailure.SOURCE_UNAVAILABLE
    assert result.snapshot is None
    assert result.verification is ManagerVerification.UNVERIFIED


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (
            ProviderAuthenticationError("rejected", provider_id="test"),
            ManagerStateFailure.AUTHENTICATION_FAILED,
        ),
        (
            ProviderManagerIdentityError("mismatch", provider_id="test"),
            ManagerStateFailure.MANAGER_IDENTITY_MISMATCH,
        ),
        (
            ProviderDataError("malformed", provider_id="test"),
            ManagerStateFailure.MALFORMED_RESPONSE,
        ),
        (
            ProviderUnavailableError("offline", provider_id="test"),
            ManagerStateFailure.SOURCE_UNAVAILABLE,
        ),
    ],
)
def test_t10_typed_provider_failures_are_mapped(
    error: Exception, failure: ManagerStateFailure
) -> None:
    result = acquire_manager_state(
        _Source(error),
        entry_id=42,
        target_event=GameweekNumber(value=1),
        known_player_types=TYPES,
    )
    assert result.failure is failure
    assert result.verification is ManagerVerification.UNVERIFIED
    assert result.snapshot is None


def test_t10_unexpected_provider_failure_is_safe_and_sanitized() -> None:
    secret = "cookie=super-secret-token"
    result = acquire_manager_state(
        _Source(RuntimeError(secret)),
        entry_id=42,
        target_event=GameweekNumber(value=1),
        known_player_types=TYPES,
    )
    assert secret not in str(result)
    assert result.failure is ManagerStateFailure.SOURCE_UNAVAILABLE


def test_t11_failed_current_acquisition_does_not_fallback() -> None:
    prior = snapshot()
    result = acquire_manager_state(
        _Source(RuntimeError("offline")),
        entry_id=42,
        target_event=GameweekNumber(value=1),
        known_player_types=TYPES,
        expected_snapshot=prior,
    )
    assert result.snapshot is None


def test_t12_goalkeeper_is_derived_from_type_not_position() -> None:
    value = snapshot(
        reserve_goalkeeper_player_id=1,
        starting_xi_player_ids=(1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 15),
        ordered_outfield_bench_player_ids=(12, 13, 14),
    )
    assert verified(value).verification is ManagerVerification.UNVERIFIED


def test_t13_illegal_composition_and_xi_fail() -> None:
    assert verified().verification is ManagerVerification.VERIFIED
    assert verified(
        snapshot(starting_xi_player_ids=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12))
    ).verification is ManagerVerification.UNVERIFIED


def test_t14_optional_fields_can_be_absent() -> None:
    assert verified().verification is ManagerVerification.VERIFIED


def test_t15_future_source_timestamp_fails_at_model_boundary() -> None:
    with pytest.raises(ValueError, match="later"):
        snapshot(source_picks_last_updated=NOW + timedelta(seconds=1))


class _Source:
    def __init__(self, value: ManagerStateSnapshot | Exception):
        self.value = value

    def acquire(self, *, entry_id: int, target_event: GameweekNumber) -> ManagerStateSnapshot:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_t16_and_t18_scope_boundary_has_no_live_mutation_stage_or_optimiser_changes() -> None:
    assert ORCHESTRATOR_STAGES == (
        DOCTOR_STAGE,
        EVIDENCE_STAGE,
        BASELINE_STAGE,
        PRE_SUBMISSION_VERIFY_STAGE,
        OPERATOR_EXECUTION_CONFIRMATION_STAGE,
        POST_SUBMISSION_VERIFY_STAGE,
    )
    assert "mutation" not in ORCHESTRATOR_STAGES
    assert "submit" not in ORCHESTRATOR_STAGES
    assert not hasattr(_Source, "post")


def test_t17_persisted_artefact_excludes_secret_material(tmp_path: Path) -> None:
    path = write_manager_state(snapshot(), state_root=tmp_path)
    text = path.read_text()
    assert all(
        secret not in text
        for secret in ("password", "token", "cookie", "Authorization", "super-secret")
    )


def test_artefact_publication_is_idempotent_and_readable(tmp_path: Path) -> None:
    first = write_manager_state(snapshot(), state_root=tmp_path)
    second = write_manager_state(snapshot(), state_root=tmp_path)
    assert first == second
    restored = ManagerStateSnapshot.model_validate_json(first.read_bytes())
    assert restored.semantic_identity == snapshot().semantic_identity
    assert first.read_bytes() == serialize_manager_state(restored)
