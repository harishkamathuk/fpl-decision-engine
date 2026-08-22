"""Use cases for the typed, atomic control-plane run-record ledger.

Every mutation loads the current record, applies one approved transition in memory and
commits it atomically. Validation happens before any write: an invalid transition or an
invalid candidate record raises and leaves the stored record untouched. Stage state
transitions follow the Issue #80 contract exactly; retries append new immutable stage
attempts and never rewrite prior attempts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from pydantic import ValidationError

from fpl_decision_engine.application.gameweek_evidence import (
    GameweekEvidenceArtifact,
    InvalidEvidenceManifest,
    parse_gameweek_evidence_manifest,
)
from fpl_decision_engine.domain.run_record import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    RUN_RECORD_SCHEMA_V2,
    AuthorityEvent,
    CloseOutcome,
    LegacyRunRecord,
    RecordedDecision,
    RunArtefact,
    RunRecord,
    RunState,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.ports.persistence import UnsupportedSchemaVersion
from fpl_decision_engine.ports.run_records import (
    InvalidPreviousRunReference,
    InvalidRunRecord,
    InvalidRunStateTransition,
    InvalidStageTransition,
    RunRecordNotFound,
    RunRecordRepository,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RunRecordValidation:
    """Validation report for one existing run record."""

    run_id: UUID
    record: RunRecord | LegacyRunRecord
    ok: bool
    issues: tuple[str, ...]


class RunRecordService:
    """Operate on the run-record ledger through typed, validated use cases."""

    def __init__(
        self,
        repository: RunRecordRepository,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    def create_run(
        self,
        *,
        run_id: UUID,
        season: str,
        gameweek: int,
        mandatory_stages: Iterable[str],
        previous_run_id: UUID | None = None,
        code_revision: str | None = None,
        config_fingerprint: str | None = None,
    ) -> RunRecord:
        """Create a provisional run with validated, explicit lineage.

        When ``previous_run_id`` is omitted it is resolved deterministically from the
        current completed authoritative run for the same season/Gameweek, per Issue #80;
        when no such run exists the run is created without a previous run.
        """

        if previous_run_id is not None:
            if self._repository.get(previous_run_id) is None:
                raise InvalidPreviousRunReference(
                    f"previous_run_id {previous_run_id} for run {run_id} does not reference "
                    "an existing run record; provide a recorded run id or omit "
                    "previous_run_id to resolve the current authoritative run for season "
                    f"{season} gameweek {gameweek}"
                )
        else:
            authoritative = self._repository.resolve_authoritative_run(
                season=season, gameweek=gameweek
            )
            if authoritative is not None:
                previous_run_id = authoritative.run_id
        record = self._new_record(
            run_id=run_id,
            season=season,
            gameweek=gameweek,
            created_at=self._now(),
            previous_run_id=previous_run_id,
            mandatory_stages=tuple(mandatory_stages),
            code_revision=code_revision,
            config_fingerprint=config_fingerprint,
        )
        self._repository.save(record, expected_raw=None)
        return record

    def get_run(self, run_id: UUID) -> RunRecord | LegacyRunRecord:
        """Read and validate one run record; legacy records are read without fabrication."""

        record = self._repository.get(run_id)
        if record is None:
            raise RunRecordNotFound(f"run record {run_id} does not exist in the ledger")
        return record

    def validate_run(self, run_id: UUID) -> RunRecordValidation:
        """Report whether an existing run record reads back as structurally valid."""

        record = self._repository.get(run_id)
        if record is None:
            raise RunRecordNotFound(f"run record {run_id} does not exist in the ledger")
        if isinstance(record, LegacyRunRecord):
            issues = list(record.parse_issues)
            issues.append(
                "legacy record without schema_version 1; absent fields are treated as "
                "unknown and are never fabricated"
            )
            return RunRecordValidation(
                run_id=run_id, record=record, ok=not issues, issues=tuple(issues)
            )
        # Structural validity is enforced by the strict parse itself; a valid v1
        # record therefore validates clean.
        return RunRecordValidation(run_id=run_id, record=record, ok=True, issues=())

    def list_runs(
        self, *, season: str | None = None, gameweek: int | None = None
    ) -> tuple[RunRecord | LegacyRunRecord, ...]:
        return self._repository.list(season=season, gameweek=gameweek)

    def start_stage(
        self, run_id: UUID, stage: str, *, by: str | None = None, note: str | None = None
    ) -> RunRecord:
        """Transition a stage PENDING -> RUNNING, creating its first attempt if needed."""

        record = self._load_current(run_id)
        self._require_provisional(record, "record stage results")
        latest = record.latest_attempt(stage)
        if latest is not None and latest.status is not StageState.PENDING:
            raise InvalidStageTransition(
                f"run {run_id} stage '{stage}' attempt {latest.attempt}: cannot start from "
                f"{latest.status.value}; only a PENDING attempt (or an unattempted stage) "
                "may start"
            )
        attempt = self._validated(
            StageAttempt,
            context=f"run {run_id} stage '{stage}' start",
            stage=stage,
            attempt=latest.attempt if latest is not None else 1,
            status=StageState.RUNNING,
            started_at=self._now(),
            note=note or (latest.note if latest is not None else None),
            by=by or (latest.by if latest is not None else None),
        )
        return self._commit(
            record,
            context=f"run {run_id} stage '{stage}' start",
            stage_attempts=_replace_attempt(record.stage_attempts, attempt),
        )

    def finish_stage(
        self,
        run_id: UUID,
        stage: str,
        status: StageState,
        *,
        by: str | None = None,
        note: str | None = None,
    ) -> RunRecord:
        """Transition the running attempt of a stage to PASS, WARN or FAIL."""

        if status not in (StageState.PASS, StageState.WARN, StageState.FAIL):
            raise InvalidStageTransition(
                f"run {run_id} stage '{stage}': finish requires PASS, WARN or FAIL, "
                f"got {status.value}"
            )
        record = self._load_current(run_id)
        self._require_provisional(record, "record stage results")
        latest = record.latest_attempt(stage)
        if latest is None or latest.status is not StageState.RUNNING:
            raise InvalidStageTransition(
                f"run {run_id} stage '{stage}': cannot finish from "
                f"{latest.status.value if latest is not None else 'no attempt'}; only a "
                "RUNNING attempt may finish"
            )
        attempt = self._validated(
            StageAttempt,
            context=f"run {run_id} stage '{stage}' finish",
            stage=stage,
            attempt=latest.attempt,
            status=status,
            started_at=latest.started_at,
            finished_at=self._now(),
            note=note or latest.note,
            by=by or latest.by,
        )
        return self._commit(
            record,
            context=f"run {run_id} stage '{stage}' finish",
            stage_attempts=_replace_attempt(record.stage_attempts, attempt),
        )

    def block_stage(
        self, run_id: UUID, stage: str, *, by: str | None = None, note: str | None = None
    ) -> RunRecord:
        """Transition a stage PENDING -> BLOCKED (prerequisites not satisfied)."""

        record = self._load_current(run_id)
        self._require_provisional(record, "record stage results")
        latest = record.latest_attempt(stage)
        if latest is not None and latest.status is not StageState.PENDING:
            raise InvalidStageTransition(
                f"run {run_id} stage '{stage}' attempt {latest.attempt}: cannot block from "
                f"{latest.status.value}; only a PENDING attempt (or an unattempted stage) "
                "may be blocked"
            )
        attempt = self._validated(
            StageAttempt,
            context=f"run {run_id} stage '{stage}' block",
            stage=stage,
            attempt=latest.attempt if latest is not None else 1,
            status=StageState.BLOCKED,
            finished_at=self._now(),
            note=note or (latest.note if latest is not None else None),
            by=by or (latest.by if latest is not None else None),
        )
        return self._commit(
            record,
            context=f"run {run_id} stage '{stage}' block",
            stage_attempts=_replace_attempt(record.stage_attempts, attempt),
        )

    def retry_stage(
        self, run_id: UUID, stage: str, *, by: str | None = None, note: str | None = None
    ) -> RunRecord:
        """Append a new PENDING attempt after a FAIL or BLOCKED latest attempt.

        Retries create new immutable stage attempts and never rewrite prior attempts.
        The approved retry path requires operator attribution (``by``), per Issue #80.
        """

        if not by:
            raise InvalidStageTransition(
                f"run {run_id} stage '{stage}': retry requires operator attribution (by)"
            )
        record = self._load_current(run_id)
        self._require_provisional(record, "record stage results")
        latest = record.latest_attempt(stage)
        if latest is None or latest.status not in (StageState.FAIL, StageState.BLOCKED):
            raise InvalidStageTransition(
                f"run {run_id} stage '{stage}': only a FAIL or BLOCKED latest attempt may "
                f"be retried; current latest attempt is "
                f"{latest.status.value if latest is not None else 'absent'}"
            )
        attempt = self._validated(
            StageAttempt,
            context=f"run {run_id} stage '{stage}' retry",
            stage=stage,
            attempt=latest.attempt + 1,
            status=StageState.PENDING,
            note=note,
            by=by,
        )
        return self._commit(
            record,
            context=f"run {run_id} stage '{stage}' retry",
            stage_attempts=_replace_attempt(record.stage_attempts, attempt),
        )

    def record_artefact(
        self,
        run_id: UUID,
        *,
        name: str,
        reference: str,
        sha256: str,
        kind: str | None = None,
    ) -> RunRecord:
        """Record an artefact reference and content hash; identical re-records are no-ops."""

        record = self._load_current(run_id)
        self._require_provisional(record, "record artefacts")
        for existing in record.artefacts:
            if existing.name == name:
                if (
                    existing.reference == reference
                    and existing.sha256 == sha256
                    and existing.kind == kind
                ):
                    return record
                raise InvalidRunRecord(
                    f"run {run_id}: artefact name '{name}' is already recorded with a "
                    "different reference or hash"
                )
        artefact = self._validated(
            RunArtefact,
            context=f"run {run_id} artefact '{name}'",
            name=name,
            reference=reference,
            sha256=sha256,
            kind=kind,
            recorded_at=self._now(),
        )
        return self._commit(
            record,
            context=f"run {run_id} artefact '{name}'",
            artefacts=record.artefacts + (artefact,),
        )

    def record_evidence_manifest(
        self,
        run_id: UUID,
        *,
        evidence_identity: str,
        artifact: GameweekEvidenceArtifact,
    ) -> RunRecord:
        """Verify persisted manifest bytes, then atomically bind their identity to a run.

        The typed artifact is the evidence persistence/reference seam. Its reference is
        resolved again here; its claimed hash and identity are both verified against the
        exact persisted bytes before the v1-to-v2 RunRecord transition is committed.
        """

        record = self._load_current(run_id)
        self._require_provisional(record, "record evidence")
        try:
            manifest_bytes = artifact.read_bytes()
        except OSError as exc:
            raise InvalidRunRecord(
                f"run {run_id}: cannot read persisted Gameweek evidence manifest "
                f"{artifact.reference!r}: {exc}"
            ) from exc
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if artifact.sha256 != manifest_sha256:
            raise InvalidRunRecord(
                f"run {run_id}: persisted Gameweek evidence manifest SHA-256 mismatch: "
                f"claimed {artifact.sha256}, computed {manifest_sha256} from "
                f"{artifact.reference!r}"
            )
        try:
            manifest = parse_gameweek_evidence_manifest(manifest_bytes)
        except (InvalidEvidenceManifest, UnsupportedSchemaVersion) as exc:
            raise InvalidRunRecord(
                f"run {run_id}: persisted Gameweek evidence manifest is invalid: {exc}"
            ) from exc
        if artifact.evidence_identity != manifest.evidence_identity:
            raise InvalidRunRecord(
                f"run {run_id}: persisted artifact claims evidence identity "
                f"{artifact.evidence_identity}, but manifest reconstructs "
                f"{manifest.evidence_identity}"
            )
        if evidence_identity != manifest.evidence_identity:
            raise InvalidRunRecord(
                f"run {run_id}: requested evidence identity {evidence_identity} does not "
                f"match persisted manifest identity {manifest.evidence_identity}"
            )
        if record.season != manifest.season or record.gameweek != manifest.gameweek.value:
            raise InvalidRunRecord(
                f"run {run_id}: evidence manifest season/Gameweek "
                f"{manifest.season}/GW{manifest.gameweek.value} does not match run "
                f"{record.season}/GW{record.gameweek}"
            )
        existing = next(
            (
                artefact
                for artefact in record.artefacts
                if artefact.kind == GAMEWEEK_EVIDENCE_ARTEFACT_KIND
            ),
            None,
        )
        if existing is not None:
            if (
                record.evidence_identity == manifest.evidence_identity
                and existing.reference == artifact.reference
                and existing.sha256 == manifest_sha256
            ):
                return record
            raise InvalidRunRecord(
                f"run {run_id}: evidence is already bound to "
                f"{record.evidence_identity} at {existing.reference}; drift requires a new run"
            )
        run_artefact = self._validated(
            RunArtefact,
            context=f"run {run_id} Gameweek evidence manifest",
            name="gameweek-evidence",
            reference=artifact.reference,
            sha256=manifest_sha256,
            kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
            recorded_at=self._now(),
        )
        return self._commit(
            record,
            context=f"run {run_id} Gameweek evidence bind",
            schema_version=RUN_RECORD_SCHEMA_V2,
            evidence_identity=manifest.evidence_identity,
            artefacts=record.artefacts + (run_artefact,),
        )

    def record_decision(
        self,
        run_id: UUID,
        *,
        reference: str,
        sha256: str | None = None,
        by: str | None = None,
        summary: str | None = None,
    ) -> RunRecord:
        """Record a decision reference; identical re-recordings are no-ops."""

        record = self._load_current(run_id)
        self._require_provisional(record, "record decisions")
        for existing in record.decisions:
            if (
                existing.reference == reference
                and existing.sha256 == sha256
                and existing.by == by
                and existing.summary == summary
            ):
                return record
        decision = self._validated(
            RecordedDecision,
            context=f"run {run_id} decision",
            reference=reference,
            sha256=sha256,
            recorded_at=self._now(),
            by=by,
            summary=summary,
        )
        return self._commit(
            record,
            context=f"run {run_id} decision",
            decisions=record.decisions + (decision,),
        )

    def close_run(
        self,
        run_id: UUID,
        *,
        outcome: CloseOutcome,
        by: str | None = None,
        note: str | None = None,
    ) -> RunRecord:
        """Close a provisional run as completed or failed, validating the #80 definitions."""

        record = self._load_current(run_id)
        if record.state is not RunState.PROVISIONAL:
            raise InvalidRunStateTransition(
                f"run {run_id}: cannot close from state {record.state.value}; only a "
                "provisional run may be closed"
            )
        if outcome is CloseOutcome.COMPLETED:
            if not record.mandatory_stages_acceptable:
                offenders: list[str] = []
                for stage in record.mandatory_stages:
                    latest = record.latest_attempt(stage)
                    if latest is None or latest.status not in (
                        StageState.PASS,
                        StageState.WARN,
                    ):
                        offenders.append(stage)
                raise InvalidRunStateTransition(
                    f"run {run_id}: cannot complete because mandatory stage(s) "
                    f"{', '.join(offenders)} lack an acceptable terminal outcome (PASS/WARN)"
                )
            state = RunState.COMPLETED
        else:
            if not record.mandatory_failure:
                raise InvalidRunStateTransition(
                    f"run {run_id}: cannot close as failed because no mandatory stage has a "
                    "FAIL or BLOCKED latest attempt"
                )
            state = RunState.FAILED
        return self._commit(
            record,
            context=f"run {run_id} close",
            state=state,
            closed_at=self._now(),
            diagnostic_summary=note,
        )

    def promote_run(self, run_id: UUID, *, by: str, reason: str) -> RunRecord:
        """Promote a completed run to authoritative via an explicit, attributable approval."""

        record = self._load_current(run_id)
        if record.state is not RunState.COMPLETED:
            raise InvalidRunStateTransition(
                f"run {run_id}: only a completed run may become authoritative; current "
                f"state is {record.state.value}"
            )
        if not by or not reason:
            raise InvalidRunStateTransition(
                f"run {run_id}: promotion requires operator attribution (by) and a reason"
            )
        event = self._validated(
            AuthorityEvent,
            context=f"run {run_id} promotion",
            approved_at=self._now(),
            by=by,
            reason=reason,
        )
        return self._commit(
            record,
            context=f"run {run_id} promotion",
            state=RunState.AUTHORITATIVE,
            authority_events=(event,),
        )

    def _load_current(self, run_id: UUID) -> RunRecord:
        record = self._repository.get(run_id)
        if record is None:
            raise RunRecordNotFound(f"run record {run_id} does not exist in the ledger")
        if isinstance(record, LegacyRunRecord):
            raise InvalidRunRecord(
                f"run {run_id} is a legacy record without schema_version 1; the typed "
                "ledger does not silently migrate it. Recreate the run with the "
                "run-record interface instead."
            )
        return record

    def _require_provisional(self, record: RunRecord, action: str) -> None:
        if record.state is not RunState.PROVISIONAL:
            raise InvalidRunStateTransition(
                f"run {record.run_id}: cannot {action} because the run is "
                f"{record.state.value}; provenance is immutable after close"
            )

    def _new_record(self, **kwargs: object) -> RunRecord:
        return self._validated(RunRecord, context="cannot create run record", **kwargs)

    def _validated(self, model_type: type[T], *, context: str, **kwargs: object) -> T:
        try:
            return model_type(**kwargs)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise InvalidRunRecord(f"{context}: {_format_validation(exc)}") from exc

    def _commit(self, record: RunRecord, *, context: str, **changes: object) -> RunRecord:
        candidate = self._rebuild(record, context=context, **changes)
        expected = self._repository.get_raw(record.run_id)
        self._repository.save(candidate, expected_raw=expected)
        return candidate

    def _rebuild(self, record: RunRecord, *, context: str, **changes: object) -> RunRecord:
        payload = record.model_dump(mode="python")
        payload.update(changes)
        try:
            return RunRecord(**payload)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise InvalidRunRecord(f"{context}: {_format_validation(exc)}") from exc


def _replace_attempt(
    attempts: tuple[StageAttempt, ...], attempt: StageAttempt
) -> tuple[StageAttempt, ...]:
    """Replace the attempt with the same (stage, attempt) or append it as a new attempt."""

    replaced = False
    updated: list[StageAttempt] = []
    for existing in attempts:
        if existing.stage == attempt.stage and existing.attempt == attempt.attempt:
            updated.append(attempt)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(attempt)
    return tuple(sorted(updated, key=lambda item: (item.stage, item.attempt)))


def _format_validation(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else str(first["msg"])
