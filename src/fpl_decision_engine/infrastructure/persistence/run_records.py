"""Atomic JSON-file run-record ledger for the control-plane provenance.

Each run is one schema-validated JSON document named ``<run_id>.json`` beneath the
ledger root. Every write validates the candidate record first, stages a temporary file
in the same directory, fsyncs it and atomically renames it over the target, so a failed
write leaves the previous record byte-for-byte intact and never leaves a partial
document behind. Concurrent modification is detected by comparing the on-disk raw
document against the snapshot the caller loaded before committing.

Selection of previous runs and of the current authoritative run is derived exclusively
from recorded document content (authority events), never from mtime, directory
ordering or operator guesswork.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from fpl_decision_engine.domain.run_record import (
    AuthorityEvent,
    LegacyRunRecord,
    RecordedDecision,
    RunArtefact,
    RunRecord,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.ports.persistence import UnsupportedSchemaVersion
from fpl_decision_engine.ports.run_records import (
    InvalidRunRecord,
    RunRecordConflict,
    RunRecordNotFound,
)

SCHEMA_VERSION = 1


def serialize_run_record(record: RunRecord) -> str:
    """Return the on-disk JSON document for a valid current-format record."""
    return record.model_dump_json(indent=2) + "\n"


def parse_run_record(raw: str) -> RunRecord | LegacyRunRecord:
    """Parse and strictly validate a run-record document.

    Documents without a ``schema_version`` are treated as legacy and read best-effort;
    documents claiming a newer schema version are rejected as unsupported rather than
    silently downgraded.
    """
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRunRecord(f"run-record file is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidRunRecord("run-record file must contain a JSON object")
    data = cast(dict[str, object], parsed)
    version = data.get("schema_version")
    if version is None:
        return parse_legacy_run_record(data)
    if isinstance(version, bool) or not isinstance(version, int):
        raise InvalidRunRecord("schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"unsupported run-record schema_version {version}; reader supports {SCHEMA_VERSION}"
        )
    try:
        return RunRecord.model_validate(data)
    except ValidationError as exc:
        raise InvalidRunRecord(f"invalid run record: {_format_validation_error(exc)}") from exc


def parse_legacy_run_record(data: dict[str, object]) -> LegacyRunRecord:
    """Read a sparse historical record without fabricating missing values.

    Every known field is parsed only when present and type-compatible; anything absent
    or unparseable remains None/empty and is reported in ``parse_issues``. The raw
    payload is preserved for operator inspection.
    """

    issues: list[str] = []

    def best_effort(field: str, parse: Callable[[object], object]) -> Any:
        if field not in data:
            return None
        try:
            return parse(data[field])
        except (TypeError, ValueError, ValidationError) as exc:
            issues.append(f"{field}: {exc}")
            return None

    def aware_timestamp(value: object) -> datetime:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return parsed

    def uuid_value(value: object) -> UUID:
        return UUID(str(value))

    def legacy_int(value: object) -> int:
        return int(str(value))

    def gameweek_value(value: object) -> int:
        parsed = legacy_int(value)
        if not 1 <= parsed <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        return parsed

    def non_blank(value: object) -> str:
        parsed = str(value).strip()
        if not parsed:
            raise ValueError("value must not be blank")
        return parsed

    def optional_text(value: object | None) -> str | None:
        return str(value) if value is not None else None

    def attempts(value: object) -> tuple[StageAttempt, ...]:
        if not isinstance(value, list):
            raise ValueError("expected a list of stage attempts")
        parsed: list[StageAttempt] = []
        for item in cast(list[object], value):
            if not isinstance(item, dict):
                issues.append("stage_attempts entry: expected an object")
                continue
            entry = cast(dict[str, object], item)
            try:
                parsed.append(
                    StageAttempt(
                        stage=non_blank(entry["stage"]),
                        attempt=legacy_int(entry["attempt"]),
                        status=StageState(non_blank(entry["status"])),
                        started_at=(
                            aware_timestamp(entry["started_at"])
                            if entry.get("started_at") is not None
                            else None
                        ),
                        finished_at=(
                            aware_timestamp(entry["finished_at"])
                            if entry.get("finished_at") is not None
                            else None
                        ),
                        note=optional_text(entry.get("note")),
                        by=optional_text(entry.get("by")),
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                issues.append(f"stage_attempts entry: {exc}")
        return tuple(parsed)

    def artefacts(value: object) -> tuple[RunArtefact, ...]:
        if not isinstance(value, list):
            raise ValueError("expected a list of artefacts")
        parsed: list[RunArtefact] = []
        for item in cast(list[object], value):
            if not isinstance(item, dict):
                issues.append("artefacts entry: expected an object")
                continue
            entry = cast(dict[str, object], item)
            try:
                parsed.append(
                    RunArtefact(
                        name=non_blank(entry["name"]),
                        reference=non_blank(entry["reference"]),
                        sha256=non_blank(entry["sha256"]),
                        kind=optional_text(entry.get("kind")),
                        recorded_at=aware_timestamp(entry["recorded_at"]),
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                issues.append(f"artefacts entry: {exc}")
        return tuple(parsed)

    def decisions(value: object) -> tuple[RecordedDecision, ...]:
        if not isinstance(value, list):
            raise ValueError("expected a list of decisions")
        parsed: list[RecordedDecision] = []
        for item in cast(list[object], value):
            if not isinstance(item, dict):
                issues.append("decisions entry: expected an object")
                continue
            entry = cast(dict[str, object], item)
            try:
                parsed.append(
                    RecordedDecision(
                        reference=non_blank(entry["reference"]),
                        sha256=optional_text(entry.get("sha256")),
                        recorded_at=aware_timestamp(entry["recorded_at"]),
                        by=optional_text(entry.get("by")),
                        summary=optional_text(entry.get("summary")),
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                issues.append(f"decisions entry: {exc}")
        return tuple(parsed)

    def authority_events(value: object) -> tuple[AuthorityEvent, ...]:
        if not isinstance(value, list):
            raise ValueError("expected a list of authority events")
        parsed: list[AuthorityEvent] = []
        for item in cast(list[object], value):
            if not isinstance(item, dict):
                issues.append("authority_events entry: expected an object")
                continue
            entry = cast(dict[str, object], item)
            try:
                parsed.append(
                    AuthorityEvent(
                        approved_at=aware_timestamp(entry["approved_at"]),
                        by=non_blank(entry["by"]),
                        reason=non_blank(entry["reason"]),
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                issues.append(f"authority_events entry: {exc}")
        return tuple(parsed)

    def mandatory_stages(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise ValueError("expected a list of stage names")
        cleaned = tuple(non_blank(item) for item in cast(list[object], value))
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("mandatory_stages must be unique")
        return cleaned

    state_value = data.get("state")
    return LegacyRunRecord(
        run_id=best_effort("run_id", uuid_value),
        season=best_effort("season", non_blank),
        gameweek=best_effort("gameweek", gameweek_value),
        created_at=best_effort("created_at", aware_timestamp),
        previous_run_id=best_effort("previous_run_id", uuid_value),
        state=state_value if isinstance(state_value, str) else None,
        mandatory_stages=best_effort("mandatory_stages", mandatory_stages) or (),
        stage_attempts=best_effort("stage_attempts", attempts) or (),
        artefacts=best_effort("artefacts", artefacts) or (),
        decisions=best_effort("decisions", decisions) or (),
        authority_events=best_effort("authority_events", authority_events) or (),
        closed_at=best_effort("closed_at", aware_timestamp),
        code_revision=best_effort("code_revision", non_blank),
        config_fingerprint=best_effort("config_fingerprint", non_blank),
        diagnostic_summary=best_effort("diagnostic_summary", non_blank),
        parse_issues=tuple(issues),
        raw=data,
    )


class RunRecordLedger:
    """One schema-validated JSON document per run, replaced atomically."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, record: RunRecord, *, expected_raw: str | None = None) -> None:
        """Atomically commit ``record``, rejecting conflicts and concurrent edits."""

        path = self._path(record.run_id)
        current = self._read_text(path)
        if expected_raw is None:
            if current is not None:
                raise RunRecordConflict(
                    f"run record {record.run_id} already exists; refusing to overwrite"
                )
        elif current is None:
            raise RunRecordNotFound(
                f"run record {record.run_id} disappeared before commit; nothing was written"
            )
        elif current != expected_raw:
            raise RunRecordConflict(
                f"run record {record.run_id} changed since it was loaded; refusing to "
                "overwrite concurrent edits"
            )
        content = serialize_run_record(record)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{record.run_id}.", suffix=".tmp", dir=self.root
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def get(self, run_id: UUID) -> RunRecord | LegacyRunRecord | None:
        """Read one run record, validating it against the current schema where possible."""

        raw = self.get_raw(run_id)
        if raw is None:
            return None
        record = parse_run_record(raw)
        if isinstance(record, RunRecord) and record.run_id != run_id:
            raise InvalidRunRecord(
                f"run-record file {self._path(run_id).name} contains run_id "
                f"{record.run_id}; filename and content disagree"
            )
        return record

    def get_raw(self, run_id: UUID) -> str | None:
        return self._read_text(self._path(run_id))

    def list(
        self, *, season: str | None = None, gameweek: int | None = None
    ) -> tuple[RunRecord | LegacyRunRecord, ...]:
        """Return all run records, optionally filtered by season/gameweek."""

        records: list[RunRecord | LegacyRunRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                run_id = UUID(path.stem)
            except ValueError:
                # Files outside the <uuid>.json naming convention are not run records.
                continue
            record = self.get(run_id)
            if record is None:
                continue
            if season is not None and record.season != season:
                continue
            if gameweek is not None and record.gameweek != gameweek:
                continue
            records.append(record)
        return tuple(records)

    def resolve_authoritative_run(self, *, season: str, gameweek: int) -> RunRecord | None:
        """Resolve the current authoritative run for a season/Gameweek from authority events.

        The current authoritative run is the one whose recorded authority approval is
        latest. Supersession is append-only: prior authority history is preserved and
        never erased. If two runs share the same approval timestamp the resolution is
        ambiguous and fails explicitly rather than guessing.
        """

        best: tuple[datetime, RunRecord] | None = None
        for path in sorted(self.root.glob("*.json")):
            try:
                run_id = UUID(path.stem)
            except ValueError:
                continue
            record = self.get(run_id)
            if not isinstance(record, RunRecord):
                continue
            if record.season != season or record.gameweek != gameweek:
                continue
            if not record.authority_events:
                continue
            approved_at = record.authority_events[-1].approved_at
            if best is None or approved_at > best[0]:
                best = (approved_at, record)
            elif approved_at == best[0]:
                raise InvalidRunRecord(
                    f"ambiguous current authoritative run for season {season} gameweek "
                    f"{gameweek}: runs {best[1].run_id} and {record.run_id} share approval "
                    "time; resolve the authority history explicitly before creating a new run"
                )
        return best[1] if best is not None else None

    def _path(self, run_id: UUID) -> Path:
        return self.root / f"{run_id}.json"

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None


def _format_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else str(first["msg"])
