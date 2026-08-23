from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain.run_record import (
    AuthorityEvent,
    RunRecord,
    RunState,
    StageAttempt,
    StageState,
)

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


def record(**overrides: object) -> RunRecord:
    payload: dict[str, object] = {
        "run_id": uuid4(),
        "season": "2026-27",
        "gameweek": 1,
        "created_at": NOW,
        "mandatory_stages": ("ingest", "optimise"),
    }
    payload.update(overrides)
    return RunRecord(**payload)


def attempt(stage: str, number: int, status: StageState, **overrides: object) -> StageAttempt:
    payload: dict[str, object] = {
        "stage": stage,
        "attempt": number,
        "status": status,
        "started_at": NOW,
        "finished_at": NOW,
    }
    payload.update(overrides)
    return StageAttempt(**payload)


def test_provisional_run_requires_no_closed_at() -> None:
    with pytest.raises(ValidationError, match="provisional"):
        record(closed_at=NOW)


def test_closed_run_requires_closed_at() -> None:
    with pytest.raises(ValidationError, match="closed_at"):
        record(
            state=RunState.COMPLETED,
            stage_attempts=(attempt("ingest", 1, StageState.PASS),),
        )


def test_completed_run_requires_acceptable_mandatory_stages() -> None:
    with pytest.raises(ValidationError, match="acceptable terminal outcome"):
        record(
            state=RunState.COMPLETED,
            closed_at=NOW,
            stage_attempts=(attempt("ingest", 1, StageState.PASS),),
        )


def test_failed_run_requires_mandatory_failure() -> None:
    with pytest.raises(ValidationError, match="FAIL or BLOCKED"):
        record(state=RunState.FAILED, closed_at=NOW)


def test_authoritative_run_requires_authority_event_and_completion() -> None:
    with pytest.raises(ValidationError, match="authority approval"):
        record(
            state=RunState.AUTHORITATIVE,
            closed_at=NOW,
            stage_attempts=(
                attempt("ingest", 1, StageState.PASS),
                attempt("optimise", 1, StageState.PASS),
            ),
        )


def test_authority_event_requires_authoritative_state() -> None:
    with pytest.raises(ValidationError, match="requires state authoritative"):
        record(
            state=RunState.COMPLETED,
            closed_at=NOW,
            stage_attempts=(
                attempt("ingest", 1, StageState.PASS),
                attempt("optimise", 1, StageState.PASS),
            ),
            authority_events=(AuthorityEvent(approved_at=NOW, by="operator", reason="ok"),),
        )


def test_attempt_numbers_must_be_consecutive_per_stage() -> None:
    with pytest.raises(ValidationError, match="consecutive"):
        record(
            stage_attempts=(
                attempt("ingest", 1, StageState.PASS),
                attempt("ingest", 3, StageState.PASS),
            )
        )


def test_self_lineage_is_rejected() -> None:
    run_id = uuid4()
    with pytest.raises(ValidationError, match="run itself"):
        record(run_id=run_id, previous_run_id=run_id)


def test_invalid_sha256_is_rejected() -> None:
    from fpl_decision_engine.domain.run_record import RunArtefact

    with pytest.raises(ValidationError, match="SHA-256"):
        RunArtefact(
            name="bundle",
            reference="state/bundles/x.json",
            sha256="z" * 64,
            recorded_at=NOW,
        )


def test_running_attempt_timestamp_shape() -> None:
    with pytest.raises(ValidationError, match="RUNNING requires"):
        attempt("ingest", 1, StageState.RUNNING, started_at=None, finished_at=None)


def test_blocked_attempt_never_starts() -> None:
    with pytest.raises(ValidationError, match="never starts"):
        attempt("ingest", 1, StageState.BLOCKED, started_at=NOW, finished_at=NOW)


def test_terminal_attempt_requires_finished_at() -> None:
    with pytest.raises(ValidationError, match="require started_at and finished_at"):
        attempt("ingest", 1, StageState.FAIL, finished_at=None)
