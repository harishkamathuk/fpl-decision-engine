"""Deterministic acceptance tests for Issue #88 submission safety ordering/identity."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest

from fpl_decision_engine.application import plan_submission, verify_submission
from fpl_decision_engine.application.submission_safety import (
    PreviousReconciliation,
    SafetyStatus,
)
from fpl_decision_engine.domain import (
    DecisionBundleV1,
    DecisionInputProvenance,
    DecisionRecommendation,
    Formation,
    GameweekNumber,
    ManagerStateFailure,
    ManagerStateResult,
    ManagerStateSnapshot,
    ManagerVerification,
    RawManagerPick,
)

NAMESPACE = UUID("12345678-1234-5678-1234-567812345678")
NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)

PLAYERS = tuple(sorted((uuid5(NAMESPACE, str(i)) for i in range(1, 16)), key=str))
STARTING = PLAYERS[:11]
BENCH = PLAYERS[11:]

# Element IDs are intentionally assigned in the reverse of canonical UUID order so
# that element-ID order can never be mistaken for str(uuid) canonical order.
ELEMENT_IDS = {player: 15 - i for i, player in enumerate(PLAYERS)}
REVERSE = {value: key for key, value in ELEMENT_IDS.items()}


def state(**changes: object) -> ManagerStateSnapshot:
    payload: dict[str, object] = {
        "source_provider": "test",
        "source_endpoint": "/state",
        "acquired_at_utc": NOW,
        "manager_entry_id": 42,
        "authenticated_entry_id": 42,
        "target_event_id": GameweekNumber(value=1),
        "target_deadline_time": NOW + timedelta(hours=1),
        "raw_picks": tuple(
            RawManagerPick(element_id=ELEMENT_IDS[player], position=position)
            for position, player in enumerate(PLAYERS, start=1)
        ),
        "squad_player_ids": tuple(
            ELEMENT_IDS[player] for player in sorted(PLAYERS, key=ELEMENT_IDS.__getitem__)
        ),
        "starting_xi_player_ids": tuple(
            ELEMENT_IDS[player] for player in sorted(STARTING, key=ELEMENT_IDS.__getitem__)
        ),
        "captain_player_id": ELEMENT_IDS[STARTING[0]],
        "vice_captain_player_id": ELEMENT_IDS[STARTING[1]],
        "reserve_goalkeeper_player_id": ELEMENT_IDS[BENCH[0]],
        "ordered_outfield_bench_player_ids": tuple(ELEMENT_IDS[player] for player in BENCH[1:]),
    }
    payload.update(changes)
    return ManagerStateSnapshot(**payload)


def result(value: ManagerStateSnapshot | None = None, failure=None) -> ManagerStateResult:
    if failure is not None:
        return ManagerStateResult(verification=ManagerVerification.UNVERIFIED, failure=failure)
    return ManagerStateResult(snapshot=value or state(), verification=ManagerVerification.VERIFIED)


def decision() -> DecisionBundleV1:
    selection = DecisionRecommendation(
        squad_ids=PLAYERS,
        starting_xi_ids=STARTING,
        captain_id=STARTING[0],
        vice_captain_id=STARTING[1],
        bench_ids=BENCH,
        formation=Formation(defenders=4, midfielders=4, forwards=2),
        squad_cost_tenths_million=1,
        bank_remaining_tenths_million=1,
        primary_objective=1.0,
        solver_status="optimal",
    )
    return DecisionBundleV1(
        decision_run_id=uuid5(NAMESPACE, "run"),
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=NOW,
        code_revision="test",
        config_fingerprint="test",
        inputs=DecisionInputProvenance(
            projection_provider="test",
            projection_source="test",
            projection_model_version="test",
            projection_generated_at=NOW,
        ),
        recommendation=selection,
    )


def test_verified_same_state_is_safe_and_previous_difference_requires_ack() -> None:
    current = result()
    safe = plan_submission(
        current,
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
        player_element_ids=ELEMENT_IDS,
    )
    assert safe.status is SafetyStatus.SAFE
    assert safe.previous_reconciliation is PreviousReconciliation.NOT_SUPPLIED

    different = plan_submission(
        current,
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
        previous_verified=state(
            captain_player_id=ELEMENT_IDS[STARTING[1]],
            vice_captain_player_id=ELEMENT_IDS[STARTING[0]],
        ),
        player_element_ids=ELEMENT_IDS,
    )
    assert different.blocking and different.acknowledgement_required
    acknowledged = plan_submission(
        current,
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
        previous_verified=state(
            captain_player_id=ELEMENT_IDS[STARTING[1]],
            vice_captain_player_id=ELEMENT_IDS[STARTING[0]],
        ),
        player_element_ids=ELEMENT_IDS,
        previous_acknowledged=True,
    )
    assert acknowledged.status is SafetyStatus.SAFE
    assert acknowledged.acknowledgement_required


@pytest.mark.parametrize("failure", list(ManagerStateFailure))
def test_unverified_current_state_is_blocked(failure: ManagerStateFailure) -> None:
    blocked = plan_submission(
        result(failure=failure),
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
        player_element_ids=ELEMENT_IDS,
    )
    assert blocked.status is SafetyStatus.BLOCKED
    assert blocked.blocking


def test_gameweek_and_missing_decision_are_blocked() -> None:
    assert plan_submission(
        result(),
        None,
        expected_entry_id=42,
        expected_gameweek=1,
        player_element_ids=ELEMENT_IDS,
    ).blocking
    assert plan_submission(
        result(state(target_event_id=GameweekNumber(value=2))),
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
        player_element_ids=ELEMENT_IDS,
    ).blocking


def test_uuid5_identical_post_exec_state_is_matched() -> None:
    verified = verify_submission(
        decision(),
        result(),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert verified.status is SafetyStatus.MATCHED
    assert not verified.blocking


def test_actual_choice_preserves_canonical_uuid5_ids() -> None:
    verified = verify_submission(
        decision(),
        result(),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert verified.actual_choice is not None
    actual = verified.actual_choice
    assert actual.squad_ids == PLAYERS
    assert actual.starting_xi_ids == STARTING
    assert actual.captain_id == STARTING[0]
    assert actual.vice_captain_id == STARTING[1]
    assert actual.bench_ids == BENCH


def test_squad_permutation_is_matched() -> None:
    permuted = state().squad_player_ids[::-1]
    assert permuted != state().squad_player_ids
    verified = verify_submission(
        decision(),
        result(state(squad_player_ids=permuted)),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert verified.status is SafetyStatus.MATCHED
    assert verified.actual_choice is not None
    assert verified.actual_choice.squad_ids == PLAYERS


def test_xi_permutation_is_matched() -> None:
    permuted = state().starting_xi_player_ids[::-1]
    assert permuted != state().starting_xi_player_ids
    verified = verify_submission(
        decision(),
        result(state(starting_xi_player_ids=permuted)),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert verified.status is SafetyStatus.MATCHED
    assert verified.actual_choice is not None
    assert verified.actual_choice.starting_xi_ids == STARTING


def test_outfield_bench_reorder_is_mismatched() -> None:
    reordered = state().ordered_outfield_bench_player_ids[::-1]
    assert reordered != state().ordered_outfield_bench_player_ids
    verified = verify_submission(
        decision(),
        result(state(ordered_outfield_bench_player_ids=reordered)),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert verified.status is SafetyStatus.MISMATCHED
    assert verified.blocking


@pytest.mark.parametrize(
    "change",
    [
        {
            "starting_xi_player_ids": tuple(
                ELEMENT_IDS[player] for player in list(STARTING[:10]) + [BENCH[2]]
            )
        },
        {
            "captain_player_id": ELEMENT_IDS[STARTING[1]],
            "vice_captain_player_id": ELEMENT_IDS[STARTING[0]],
        },
        {"squad_player_ids": tuple(range(1, 14)) + (16, 17)},
    ],
)
def test_post_execution_selection_difference_is_mismatched(change: dict[str, object]) -> None:
    observed = verify_submission(
        decision(),
        result(state(**change)),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert observed.status is SafetyStatus.MISMATCHED
    assert observed.blocking


def test_missing_mapping_blocks_explicitly() -> None:
    blocked = plan_submission(
        result(),
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
    )
    assert blocked.status is SafetyStatus.BLOCKED
    assert "mapping" in blocked.details[-1]


def test_duplicate_mapping_blocks_explicitly() -> None:
    mapping = dict(ELEMENT_IDS)
    mapping[PLAYERS[1]] = ELEMENT_IDS[PLAYERS[0]]
    blocked = plan_submission(
        result(),
        decision(),
        expected_entry_id=42,
        expected_gameweek=1,
        player_element_ids=mapping,
    )
    assert blocked.status is SafetyStatus.BLOCKED
    assert "ambiguous" in blocked.details[-1]


def test_missing_reverse_mapping_is_fail_closed() -> None:
    incomplete = dict(REVERSE)
    incomplete.pop(ELEMENT_IDS[PLAYERS[-1]])
    observed = verify_submission(
        decision(),
        result(),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=incomplete,
    )
    assert observed.status is SafetyStatus.MISMATCHED
    assert observed.blocking


def test_duplicate_reverse_mapping_is_fail_closed() -> None:
    duplicate = dict(REVERSE)
    duplicate[ELEMENT_IDS[PLAYERS[0]]] = PLAYERS[1]
    observed = verify_submission(
        decision(),
        result(),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=duplicate,
    )
    assert observed.status is SafetyStatus.MISMATCHED
    assert observed.blocking


def test_post_execution_failure_has_no_cached_fallback() -> None:
    observed = verify_submission(
        decision(),
        result(failure=ManagerStateFailure.SOURCE_UNAVAILABLE),
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE,
    )
    assert observed.status is SafetyStatus.SOURCE_UNAVAILABLE
    assert observed.actual_choice is None
