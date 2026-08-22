from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain.run_record import (
    CloseOutcome,
    LegacyRunRecord,
    RunRecord,
    RunState,
    StageState,
)
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger
from fpl_decision_engine.ports.run_records import (
    InvalidPreviousRunReference,
    InvalidRunRecord,
    InvalidRunStateTransition,
    InvalidStageTransition,
    RunRecordNotFound,
)

START = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self._current = start

    def __call__(self) -> datetime:
        self._current += timedelta(minutes=1)
        return self._current


@pytest.fixture
def ledger(tmp_path: Path) -> RunRecordLedger:
    return RunRecordLedger(tmp_path / "state" / "run-records")


@pytest.fixture
def service(ledger: RunRecordLedger) -> RunRecordService:
    return RunRecordService(ledger, now=FakeClock())


def create_run(
    service: RunRecordService,
    *,
    run_id: UUID | None = None,
    season: str = "2026-27",
    gameweek: int = 1,
    previous_run_id: UUID | None = None,
    mandatory_stages: tuple[str, ...] = ("ingest", "optimise"),
) -> RunRecord:
    return service.create_run(
        run_id=run_id or uuid4(),
        season=season,
        gameweek=gameweek,
        previous_run_id=previous_run_id,
        mandatory_stages=mandatory_stages,
        code_revision="deadbeef",
        config_fingerprint="sha256:config",
    )


def write_legacy(ledger: RunRecordLedger, payload: dict[str, object]) -> UUID:
    run_id = uuid4()
    payload = {**payload, "run_id": str(run_id)}
    (ledger.root / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_id


# 1. Valid run creation
def test_valid_run_creation(service: RunRecordService) -> None:
    run = create_run(service)

    assert run.state is RunState.PROVISIONAL
    assert run.previous_run_id is None
    assert run.mandatory_stages == ("ingest", "optimise")
    assert run.stage_attempts == ()
    assert run.closed_at is None


# 2. Reading a valid current-format run
def test_read_valid_current_format_run(service: RunRecordService) -> None:
    run = create_run(service)

    loaded = service.get_run(run.run_id)

    assert isinstance(loaded, RunRecord)
    assert loaded == run


def test_read_missing_run_is_actionable(service: RunRecordService) -> None:
    run_id = uuid4()
    with pytest.raises(RunRecordNotFound, match=str(run_id)):
        service.get_run(run_id)


# 3. Backward-compatible reading of a sparse historical record
def test_backward_compatible_read_of_sparse_historical_record(
    ledger: RunRecordLedger, service: RunRecordService
) -> None:
    run_id = write_legacy(
        ledger,
        {
            "run_id": str(uuid4()),
            "season": "2026-27",
            "gameweek": 1,
            "created_at": "2026-08-14T10:00:00+00:00",
            "status": "done",
            "stages": [{"name": "ingest", "result": "pass"}],
        },
    )

    loaded = service.get_run(run_id)

    assert isinstance(loaded, LegacyRunRecord)
    assert loaded.run_id == run_id
    assert loaded.season == "2026-27"
    assert loaded.gameweek == 1
    assert loaded.created_at is not None
    assert loaded.raw["status"] == "done"
    assert loaded.raw["stages"] == [{"name": "ingest", "result": "pass"}]


# 4. Recording a valid stage transition
def test_valid_stage_transition(service: RunRecordService) -> None:
    run = create_run(service)

    running = service.start_stage(run.run_id, "ingest")
    assert running.latest_attempt("ingest").status is StageState.RUNNING

    passed = service.finish_stage(run.run_id, "ingest", StageState.PASS)
    latest = passed.latest_attempt("ingest")
    assert latest.status is StageState.PASS
    assert latest.attempt == 1
    assert latest.started_at is not None and latest.finished_at is not None


# 5. Rejection of an invalid transition
def test_invalid_stage_transition_rejected(service: RunRecordService) -> None:
    run = create_run(service)

    with pytest.raises(InvalidStageTransition, match="only a RUNNING attempt may finish"):
        service.finish_stage(run.run_id, "ingest", StageState.PASS)

    service.start_stage(run.run_id, "ingest")
    with pytest.raises(InvalidStageTransition, match="cannot start from running"):
        service.start_stage(run.run_id, "ingest")

    service.finish_stage(run.run_id, "ingest", StageState.PASS)
    with pytest.raises(InvalidStageTransition, match="only a FAIL or BLOCKED latest attempt"):
        service.retry_stage(run.run_id, "ingest", by="operator")


def test_stage_transition_rejected_after_close(service: RunRecordService) -> None:
    run = create_run(service)
    service.start_stage(run.run_id, "ingest")
    service.finish_stage(run.run_id, "ingest", StageState.PASS)
    service.start_stage(run.run_id, "optimise")
    service.finish_stage(run.run_id, "optimise", StageState.PASS)
    service.close_run(run.run_id, outcome=CloseOutcome.COMPLETED)

    with pytest.raises(InvalidRunStateTransition, match="immutable after close"):
        service.start_stage(run.run_id, "ingest")


# 6. Artefact/hash recording
def test_artefact_hash_recording(service: RunRecordService) -> None:
    run = create_run(service)

    recorded = service.record_artefact(
        run.run_id,
        name="recommendation-bundle",
        reference="state/decision-bundles/2026-27/gw1/abc.json",
        sha256="a" * 64,
        kind="decision-bundle",
    )
    assert len(recorded.artefacts) == 1
    assert recorded.artefacts[0].name == "recommendation-bundle"
    assert recorded.artefacts[0].sha256 == "a" * 64

    again = service.record_artefact(
        run.run_id,
        name="recommendation-bundle",
        reference="state/decision-bundles/2026-27/gw1/abc.json",
        sha256="a" * 64,
        kind="decision-bundle",
    )
    assert len(again.artefacts) == 1

    with pytest.raises(InvalidRunRecord, match="different reference or hash"):
        service.record_artefact(
            run.run_id,
            name="recommendation-bundle",
            reference="state/decision-bundles/2026-27/gw1/abc.json",
            sha256="b" * 64,
        )


def test_artefact_invalid_hash_rejected(service: RunRecordService) -> None:
    run = create_run(service)
    with pytest.raises(InvalidRunRecord, match="SHA-256"):
        service.record_artefact(
            run.run_id, name="bundle", reference="ref", sha256="z" * 64
        )


# 7. Valid previous_run_id
def test_valid_explicit_previous_run_id(service: RunRecordService) -> None:
    first = create_run(service)

    second = create_run(service, previous_run_id=first.run_id)

    assert second.previous_run_id == first.run_id


# 8. Invalid/missing previous_run_id
def test_invalid_previous_run_id_rejected(service: RunRecordService) -> None:
    missing = uuid4()
    with pytest.raises(InvalidPreviousRunReference, match=str(missing)):
        create_run(service, previous_run_id=missing)

    assert service.list_runs() == ()


def test_previous_run_resolves_to_authoritative_when_omitted(service: RunRecordService) -> None:
    first = create_run(service)
    service.start_stage(first.run_id, "ingest")
    service.finish_stage(first.run_id, "ingest", StageState.PASS)
    service.start_stage(first.run_id, "optimise")
    service.finish_stage(first.run_id, "optimise", StageState.PASS)
    service.close_run(first.run_id, outcome=CloseOutcome.COMPLETED)
    service.promote_run(first.run_id, by="operator", reason="GW1 final run")

    second = create_run(service)

    assert second.previous_run_id == first.run_id


def test_previous_run_absent_when_no_authoritative_run(service: RunRecordService) -> None:
    run = create_run(service)
    assert run.previous_run_id is None


# 9. Run closure/completion
def test_close_completed_requires_acceptable_mandatory_stages(service: RunRecordService) -> None:
    run = create_run(service)
    service.start_stage(run.run_id, "ingest")
    service.finish_stage(run.run_id, "ingest", StageState.PASS)

    with pytest.raises(InvalidRunStateTransition, match="optimise"):
        service.close_run(run.run_id, outcome=CloseOutcome.COMPLETED)


def test_close_completed(service: RunRecordService) -> None:
    run = create_run(service)
    for stage in run.mandatory_stages:
        service.start_stage(run.run_id, stage)
        service.finish_stage(run.run_id, stage, StageState.PASS)

    closed = service.close_run(run.run_id, outcome=CloseOutcome.COMPLETED)

    assert closed.state is RunState.COMPLETED
    assert closed.closed_at is not None


def test_close_failed(service: RunRecordService) -> None:
    run = create_run(service)
    service.block_stage(run.run_id, "ingest", note="snapshot unavailable")

    failed = service.close_run(run.run_id, outcome=CloseOutcome.FAILED)

    assert failed.state is RunState.FAILED
    assert failed.closed_at is not None


def test_close_failed_without_mandatory_failure_rejected(service: RunRecordService) -> None:
    run = create_run(service)
    with pytest.raises(InvalidRunStateTransition, match="no mandatory stage has a FAIL"):
        service.close_run(run.run_id, outcome=CloseOutcome.FAILED)


# 10. Atomic persistence (ledger-level coverage in test_run_record_ledger.py)


# 11. Validation failure does not corrupt the prior record
def test_validation_failure_does_not_corrupt_prior_record(
    ledger: RunRecordLedger, service: RunRecordService
) -> None:
    run = create_run(service)
    service.start_stage(run.run_id, "ingest")
    service.finish_stage(run.run_id, "ingest", StageState.PASS)
    before = service.get_run(run.run_id)
    raw_before = ledger.get_raw(run.run_id)

    with pytest.raises(InvalidStageTransition):
        service.finish_stage(run.run_id, "ingest", StageState.FAIL)

    assert ledger.get_raw(run.run_id) == raw_before
    assert service.get_run(run.run_id) == before


# 12. Missing historical fields are not fabricated
def test_missing_historical_fields_not_fabricated(
    ledger: RunRecordLedger, service: RunRecordService
) -> None:
    run_id = write_legacy(
        ledger,
        {
            "run_id": str(uuid4()),
            "season": "2026-27",
            "gameweek": 1,
            "created_at": "2026-08-14T10:00:00+00:00",
        },
    )

    loaded = service.get_run(run_id)

    assert isinstance(loaded, LegacyRunRecord)
    assert loaded.previous_run_id is None
    assert loaded.state is None
    assert loaded.mandatory_stages == ()
    assert loaded.stage_attempts == ()
    assert loaded.artefacts == ()
    assert loaded.decisions == ()
    assert loaded.authority_events == ()
    assert loaded.closed_at is None
    assert loaded.code_revision is None
    assert loaded.diagnostic_summary is None


def test_legacy_unparseable_field_is_reported_not_fabricated(
    ledger: RunRecordLedger, service: RunRecordService
) -> None:
    run_id = write_legacy(
        ledger,
        {
            "run_id": str(uuid4()),
            "season": "2026-27",
            "gameweek": 1,
            "created_at": "2026-08-14T10:00:00+00:00",
            "stage_attempts": [
                {
                    "stage": "ingest",
                    "attempt": 1,
                    "status": "pass",
                    "started_at": "2026-08-14T10:00:00+00:00",
                    "finished_at": "2026-08-14T11:00:00+00:00",
                },
                {"stage": "broken", "attempt": "nope"},
            ],
        },
    )

    loaded = service.get_run(run_id)

    assert isinstance(loaded, LegacyRunRecord)
    assert len(loaded.stage_attempts) == 1
    assert loaded.stage_attempts[0].stage == "ingest"
    assert loaded.parse_issues and any("stage_attempts" in issue for issue in loaded.parse_issues)


# Additional transition coverage
def test_retry_appends_new_attempt_and_never_rewrites_prior(
    service: RunRecordService,
) -> None:
    run = create_run(service)
    service.block_stage(run.run_id, "ingest", note="missing evidence")

    retried = service.retry_stage(run.run_id, "ingest", by="operator", note="evidence supplied")
    attempts = retried.stage_attempts
    assert len(attempts) == 2
    assert attempts[0].status is StageState.BLOCKED
    assert attempts[0].attempt == 1
    assert attempts[1].status is StageState.PENDING
    assert attempts[1].attempt == 2
    assert attempts[1].by == "operator"

    running = service.start_stage(run.run_id, "ingest")
    assert running.latest_attempt("ingest").attempt == 2
    assert running.latest_attempt("ingest").status is StageState.RUNNING


def test_retry_requires_attribution(service: RunRecordService) -> None:
    run = create_run(service)
    service.block_stage(run.run_id, "ingest")
    with pytest.raises(InvalidStageTransition, match="attribution"):
        service.retry_stage(run.run_id, "ingest")


def test_block_and_retry_cycle_then_complete(service: RunRecordService) -> None:
    run = create_run(service)
    service.block_stage(run.run_id, "ingest")
    service.retry_stage(run.run_id, "ingest", by="operator")
    service.start_stage(run.run_id, "ingest")
    service.finish_stage(run.run_id, "ingest", StageState.PASS)
    service.start_stage(run.run_id, "optimise")
    service.finish_stage(run.run_id, "optimise", StageState.WARN)

    closed = service.close_run(run.run_id, outcome=CloseOutcome.COMPLETED)
    assert closed.state is RunState.COMPLETED


def test_promote_requires_completed(service: RunRecordService) -> None:
    run = create_run(service)
    with pytest.raises(InvalidRunStateTransition, match="only a completed run"):
        service.promote_run(run.run_id, by="operator", reason="final")


def test_promote_requires_attribution_and_reason(service: RunRecordService) -> None:
    run = create_run(service)
    for stage in run.mandatory_stages:
        service.start_stage(run.run_id, stage)
        service.finish_stage(run.run_id, stage, StageState.PASS)
    service.close_run(run.run_id, outcome=CloseOutcome.COMPLETED)

    with pytest.raises(InvalidRunStateTransition, match="attribution"):
        service.promote_run(run.run_id, by="", reason="final")


def test_promote_supersedes_and_resolution_stays_deterministic(service: RunRecordService) -> None:
    def complete_and_promote(run_id: UUID) -> None:
        for stage in ("ingest", "optimise"):
            service.start_stage(run_id, stage)
            service.finish_stage(run_id, stage, StageState.PASS)
        service.close_run(run_id, outcome=CloseOutcome.COMPLETED)
        service.promote_run(run_id, by="operator", reason="final")

    first = create_run(service)
    complete_and_promote(first.run_id)
    second = create_run(service, previous_run_id=first.run_id)
    complete_and_promote(second.run_id)

    resolved = service._repository.resolve_authoritative_run(season="2026-27", gameweek=1)
    assert resolved is not None
    assert resolved.run_id == second.run_id


def test_legacy_record_rejected_for_mutation(
    ledger: RunRecordLedger, service: RunRecordService
) -> None:
    run_id = write_legacy(ledger, {"run_id": str(uuid4()), "season": "2026-27", "gameweek": 1})

    with pytest.raises(InvalidRunRecord, match="legacy"):
        service.start_stage(run_id, "ingest")


def test_list_filters_by_season_and_gameweek(service: RunRecordService) -> None:
    gw1 = create_run(service, gameweek=1)
    gw2 = create_run(service, gameweek=2, season="2026-27")

    assert {run.run_id for run in service.list_runs(gameweek=1)} == {gw1.run_id}
    assert {run.run_id for run in service.list_runs(season="2026-27")} == {
        gw1.run_id,
        gw2.run_id,
    }
    assert service.list_runs(season="2025-26") == ()


def test_validate_run_reports_legacy_informational(
    ledger: RunRecordLedger, service: RunRecordService
) -> None:
    run_id = write_legacy(ledger, {"run_id": str(uuid4()), "season": "2026-27", "gameweek": 1})

    report = service.validate_run(run_id)

    assert not report.ok
    assert any("legacy" in issue for issue in report.issues)


def test_validate_run_ok_for_valid_current_record(service: RunRecordService) -> None:
    run = create_run(service)
    report = service.validate_run(run.run_id)
    assert report.ok
    assert report.issues == ()


def test_recording_decisions(service: RunRecordService) -> None:
    run = create_run(service)
    recorded = service.record_decision(
        run.run_id,
        reference="state/decision-bundles/2026-27/gw1/abc.json",
        sha256="a" * 64,
        by="operator",
        summary="final recommendation",
    )
    assert len(recorded.decisions) == 1
    assert recorded.decisions[0].reference.endswith("abc.json")

    duplicate = service.record_decision(
        run.run_id,
        reference="state/decision-bundles/2026-27/gw1/abc.json",
        sha256="a" * 64,
        by="operator",
        summary="final recommendation",
    )
    assert len(duplicate.decisions) == 1
