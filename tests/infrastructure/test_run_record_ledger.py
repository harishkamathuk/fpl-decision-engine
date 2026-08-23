from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from fpl_decision_engine.domain.run_record import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    AuthorityEvent,
    LegacyRunRecord,
    RunArtefact,
    RunRecord,
    RunState,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger
from fpl_decision_engine.ports.persistence import UnsupportedSchemaVersion
from fpl_decision_engine.ports.run_records import (
    InvalidRunRecord,
    RunRecordConflict,
    RunRecordNotFound,
)

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


def record(state: RunState = RunState.PROVISIONAL, **overrides: object) -> RunRecord:
    payload: dict[str, object] = {
        "run_id": uuid4(),
        "season": "2026-27",
        "gameweek": 1,
        "created_at": NOW,
        "mandatory_stages": ("ingest",),
    }
    if state is not RunState.PROVISIONAL:
        payload["state"] = state
        payload["closed_at"] = NOW
        payload["stage_attempts"] = (
            StageAttempt(
                stage="ingest",
                attempt=1,
                status=StageState.PASS,
                started_at=NOW,
                finished_at=NOW,
            ),
        )
        if state is RunState.AUTHORITATIVE:
            payload["authority_events"] = (
                AuthorityEvent(approved_at=NOW, by="operator", reason="final"),
            )
    payload.update(overrides)
    return RunRecord(**payload)


V1_WIRE_KEYS = {
    "artefacts",
    "authority_events",
    "closed_at",
    "code_revision",
    "config_fingerprint",
    "created_at",
    "decisions",
    "diagnostic_summary",
    "gameweek",
    "mandatory_stages",
    "previous_run_id",
    "run_id",
    "schema_version",
    "season",
    "stage_attempts",
    "state",
}


def test_current_unbound_record_defaults_to_schema_v2_and_round_trips(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    original = record()

    ledger.save(original)
    loaded = ledger.get(original.run_id)
    payload = json.loads(ledger.get_raw(original.run_id) or "{}")

    assert loaded == original
    assert isinstance(loaded, RunRecord)
    assert payload["schema_version"] == 2
    assert payload["evidence_identity"] is None


def test_schema_v1_wire_keys_are_frozen_and_read_does_not_rewrite(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    original = record(schema_version=1)
    ledger.save(original)
    raw_before = ledger.get_raw(original.run_id)
    payload = json.loads(raw_before or "{}")

    loaded = ledger.get(original.run_id)

    assert set(payload) == V1_WIRE_KEYS
    assert "evidence_identity" not in payload
    assert isinstance(loaded, RunRecord)
    assert loaded.schema_version == 1
    assert loaded.evidence_identity is None
    assert ledger.get_raw(original.run_id) == raw_before


def test_schema_v1_parser_rejects_v2_field_even_when_null(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    original = record(schema_version=1)
    ledger.save(original)
    payload = json.loads(ledger.get_raw(original.run_id) or "{}")
    payload["evidence_identity"] = None
    (ledger.root / f"{original.run_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidRunRecord, match="v2-only field"):
        ledger.get(original.run_id)


def test_schema_v2_unbound_round_trip_is_valid(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    original = record(schema_version=2, evidence_identity=None, artefacts=())

    ledger.save(original)

    loaded = ledger.get(original.run_id)
    payload = json.loads(ledger.get_raw(original.run_id) or "{}")
    assert loaded == original
    assert payload["schema_version"] == 2
    assert payload["evidence_identity"] is None


def test_schema_v2_bound_round_trip_requires_and_preserves_evidence(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    identity = f"sha256:{'e' * 64}"
    evidence_artefact = RunArtefact(
        name="gameweek-evidence",
        reference="/state/evidence.json",
        sha256="a" * 64,
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
        recorded_at=NOW,
    )
    original = record(
        schema_version=2,
        evidence_identity=identity,
        artefacts=(evidence_artefact,),
    )

    ledger.save(original)

    assert ledger.get(original.run_id) == original


def test_schema_versions_reject_crossed_evidence_shapes() -> None:
    evidence_artefact = RunArtefact(
        name="gameweek-evidence",
        reference="/state/evidence.json",
        sha256="a" * 64,
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
        recorded_at=NOW,
    )

    with pytest.raises(ValueError, match="schema_version 1 cannot contain"):
        record(
            schema_version=1,
            evidence_identity=f"sha256:{'e' * 64}",
            artefacts=(evidence_artefact,),
        )
    with pytest.raises(ValueError, match="requires exactly one"):
        record(schema_version=2, evidence_identity=f"sha256:{'e' * 64}")
    with pytest.raises(ValueError, match="requires evidence_identity"):
        record(schema_version=2, artefacts=(evidence_artefact,))


def test_schema_v2_bound_record_rejects_multiple_evidence_artefacts() -> None:
    first = RunArtefact(
        name="gameweek-evidence",
        reference="/state/evidence-a.json",
        sha256="a" * 64,
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
        recorded_at=NOW,
    )
    second = RunArtefact(
        name="gameweek-evidence-copy",
        reference="/state/evidence-b.json",
        sha256="b" * 64,
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
        recorded_at=NOW,
    )

    with pytest.raises(ValueError, match="requires exactly one"):
        record(
            schema_version=2,
            evidence_identity=f"sha256:{'e' * 64}",
            artefacts=(first, second),
        )


def test_create_conflict_rejected(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run = record()
    ledger.save(run)

    with pytest.raises(RunRecordConflict, match="already exists"):
        ledger.save(run, expected_raw=None)


def test_stale_expected_raw_rejected(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run = record()
    ledger.save(run)

    with pytest.raises(RunRecordConflict, match="changed since it was loaded"):
        ledger.save(run, expected_raw="stale content")


def test_atomic_write_failure_leaves_prior_record_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run = record()
    ledger.save(run)
    raw_before = ledger.get_raw(run.run_id)

    completed = record(
        run_id=run.run_id,
        season=run.season,
        gameweek=run.gameweek,
        created_at=run.created_at,
        state=RunState.COMPLETED,
    )

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        ledger.save(completed, expected_raw=raw_before)

    monkeypatch.undo()
    assert ledger.get_raw(run.run_id) == raw_before
    assert list(ledger.root.glob("*.tmp")) == []
    assert list(ledger.root.glob(".*.tmp")) == []


def test_corrupt_json_rejected_on_read(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run_id = uuid4()
    (ledger.root / f"{run_id}.json").write_text('{"run_id": ', encoding="utf-8")

    with pytest.raises(InvalidRunRecord, match="not valid JSON"):
        ledger.get(run_id)


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run_id = uuid4()
    (ledger.root / f"{run_id}.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersion, match="schema_version 3"):
        ledger.get(run_id)


def test_filename_content_mismatch_detected(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run = record()
    ledger.save(run)
    other_id = uuid4()
    (ledger.root / f"{other_id}.json").write_text(
        json.dumps(json.loads(ledger.get_raw(run.run_id) or "{}")), encoding="utf-8"
    )

    with pytest.raises(InvalidRunRecord, match="filename and content disagree"):
        ledger.get(other_id)


def test_legacy_document_read_without_schema_version(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run_id = uuid4()
    payload = {"run_id": str(run_id), "season": "2026-27", "gameweek": 1}
    (ledger.root / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = ledger.get(run_id)

    assert isinstance(loaded, LegacyRunRecord)
    assert loaded.run_id == run_id
    assert loaded.created_at is None
    assert loaded.state is None


def test_resolve_authoritative_picks_latest_approval(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    first = record(
        state=RunState.AUTHORITATIVE,
        authority_events=(AuthorityEvent(approved_at=NOW, by="operator", reason="final"),),
    )
    second = record(
        state=RunState.AUTHORITATIVE,
        authority_events=(
            AuthorityEvent(
                approved_at=datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
                by="operator",
                reason="re-run",
            ),
        ),
    )
    ledger.save(first)
    ledger.save(second)

    resolved = ledger.resolve_authoritative_run(season="2026-27", gameweek=1)

    assert resolved is not None
    assert resolved.run_id == second.run_id
    # Supersession never erases prior authority history.
    assert ledger.get(first.run_id).authority_events  # type: ignore[union-attr]


def test_ambiguous_authority_times_rejected(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    first = record(state=RunState.AUTHORITATIVE)
    second = record(state=RunState.AUTHORITATIVE)
    ledger.save(first)
    ledger.save(second)

    with pytest.raises(InvalidRunRecord, match="ambiguous"):
        ledger.resolve_authoritative_run(season="2026-27", gameweek=1)


def test_resolve_ignores_provisional_and_other_season(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    authoritative = record(state=RunState.AUTHORITATIVE)
    provisional = record()
    other_season = record(state=RunState.AUTHORITATIVE, season="2025-26")
    ledger.save(authoritative)
    ledger.save(provisional)
    ledger.save(other_season)

    resolved = ledger.resolve_authoritative_run(season="2026-27", gameweek=1)

    assert resolved is not None
    assert resolved.run_id == authoritative.run_id


def test_missing_run_is_none_and_disappeared_write_is_actionable(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    run = record()
    ledger.save(run)

    assert ledger.get(uuid4()) is None

    raw = ledger.get_raw(run.run_id)
    (ledger.root / f"{run.run_id}.json").unlink()
    with pytest.raises(RunRecordNotFound, match="disappeared"):
        ledger.save(run, expected_raw=raw)


def test_list_skips_non_run_files_and_filters(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    (ledger.root / "notes.json").write_text("{}", encoding="utf-8")
    gw1 = record()
    gw2 = record(gameweek=2)
    ledger.save(gw1)
    ledger.save(gw2)

    assert {item.run_id for item in ledger.list()} == {gw1.run_id, gw2.run_id}
    assert {item.run_id for item in ledger.list(gameweek=1)} == {gw1.run_id}
    assert ledger.list(season="2025-26") == ()
