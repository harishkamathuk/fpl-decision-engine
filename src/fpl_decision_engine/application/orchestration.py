"""Resumable orchestration for the bounded Touchline Gameweek workflow.

The first orchestrator deliberately owns only stage control. Doctor diagnostics,
immutable evidence verification and baseline optimisation remain behind their existing
application seams; stage attempts and results are persisted exclusively through the
typed RunRecord ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fpl_decision_engine.application.doctor import (
    DiagnosticStatus,
    DoctorReport,
)
from fpl_decision_engine.application.gameweek_evidence import GameweekEvidenceArtifact
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain.decision_bundle import DecisionRecommendation
from fpl_decision_engine.domain.run_record import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    CloseOutcome,
    LegacyRunRecord,
    RunRecord,
    RunState,
    StageState,
)

DOCTOR_STAGE = "doctor"
EVIDENCE_STAGE = "evidence"
BASELINE_STAGE = "baseline"
ORCHESTRATOR_STAGES = (DOCTOR_STAGE, EVIDENCE_STAGE, BASELINE_STAGE)
_ORCHESTRATOR_ACTOR = "touchline-orchestrator"


class DoctorRunner(Protocol):
    """Run the existing deterministic doctor seam."""

    def run(self) -> DoctorReport: ...


@dataclass(frozen=True, slots=True)
class BaselineOutcome:
    """Semantic recommendation and immutable artifact produced by the baseline seam."""

    recommendation: DecisionRecommendation
    reference: str
    sha256: str
    summary: str


class BaselineRunner(Protocol):
    """Execute the existing baseline path for one evidence-bound RunRecord."""

    def run(
        self,
        *,
        record: RunRecord,
        evidence_artifact: GameweekEvidenceArtifact,
    ) -> BaselineOutcome: ...


@dataclass(frozen=True, slots=True)
class OrchestratorRequest:
    """Immutable invocation identity for a fresh run or explicit resume."""

    run_id: UUID
    season: str
    gameweek: int
    code_revision: str
    config_fingerprint: str
    evidence_artifact: GameweekEvidenceArtifact
    resume: bool = False


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    """Persisted outcome and concise operator guidance for one invocation."""

    record: RunRecord
    recommendation: DecisionRecommendation | None
    next_action: str

    @property
    def exit_code(self) -> int:
        return 0 if self.record.state is RunState.COMPLETED else 1

    @property
    def stage_statuses(self) -> tuple[tuple[str, StageState | None], ...]:
        return tuple(
            (
                stage,
                (
                    latest.status
                    if (latest := self.record.latest_attempt(stage)) is not None
                    else None
                ),
            )
            for stage in ORCHESTRATOR_STAGES
        )


class OrchestratorError(RuntimeError):
    """Base error for deterministic orchestration rejection."""


class OrchestratorInputError(OrchestratorError):
    """Resume or invocation arguments conflict with immutable run provenance."""


class OrchestratorResumeError(OrchestratorError):
    """An existing run cannot safely continue through approved ledger transitions."""


class GameweekOrchestrator:
    """Execute or resume the bounded doctor → evidence → baseline stage machine."""

    def __init__(
        self,
        records: RunRecordService,
        *,
        doctor: DoctorRunner,
        baseline: BaselineRunner,
    ) -> None:
        self._records = records
        self._doctor = doctor
        self._baseline = baseline

    def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        """Execute a fresh run or resume the same persisted run and evidence.

        FAIL/BLOCKED attempts remain on a provisional run and resume through the
        existing append-only retry transition. PASS/WARN predecessors are never rerun.
        A RUNNING attempt cannot be guessed stale because #81 defines no safe recovery
        transition for it; such a run is rejected with an explicit diagnostic.
        """

        if not request.code_revision.strip() or not request.config_fingerprint.strip():
            raise OrchestratorInputError(
                "code_revision and config_fingerprint must be non-blank"
            )
        record = (
            self._load_resume(request)
            if request.resume
            else self._records.create_run(
                run_id=request.run_id,
                season=request.season,
                gameweek=request.gameweek,
                mandatory_stages=ORCHESTRATOR_STAGES,
                code_revision=request.code_revision,
                config_fingerprint=request.config_fingerprint,
            )
        )
        if request.resume:
            self._validate_resume(record, request)

        if not self._run_doctor_stage(record.run_id):
            return self._result(record.run_id, recommendation=None)
        if not self._run_evidence_stage(record.run_id, request.evidence_artifact):
            return self._result(record.run_id, recommendation=None)
        outcome = self._run_baseline_stage(record.run_id, request.evidence_artifact)
        record = self._current(record.run_id)
        baseline_attempt = record.latest_attempt(BASELINE_STAGE)
        if baseline_attempt is None or baseline_attempt.status not in (
            StageState.PASS,
            StageState.WARN,
        ):
            return self._result(record.run_id, recommendation=None)

        if record.state is RunState.PROVISIONAL:
            self._records.close_run(record.run_id, outcome=CloseOutcome.COMPLETED)
        return self._result(
            record.run_id,
            recommendation=outcome.recommendation if outcome is not None else None,
        )

    def _load_resume(self, request: OrchestratorRequest) -> RunRecord:
        record = self._records.get_run(request.run_id)
        if isinstance(record, LegacyRunRecord):
            raise OrchestratorResumeError(
                f"run {request.run_id} is legacy and cannot be resumed without fabrication"
            )
        return record

    def _validate_resume(self, record: RunRecord, request: OrchestratorRequest) -> None:
        mismatches: list[str] = []
        for name, actual, requested in (
            ("season", record.season, request.season),
            ("gameweek", record.gameweek, request.gameweek),
            ("code_revision", record.code_revision, request.code_revision),
            ("config_fingerprint", record.config_fingerprint, request.config_fingerprint),
        ):
            if actual != requested:
                mismatches.append(f"{name}: recorded={actual!r}, requested={requested!r}")
        if record.mandatory_stages != ORCHESTRATOR_STAGES:
            mismatches.append(
                "mandatory_stages: recorded="
                f"{record.mandatory_stages!r}, required={ORCHESTRATOR_STAGES!r}"
            )
        if mismatches:
            raise OrchestratorInputError(
                f"run {record.run_id} resume argument drift: " + "; ".join(mismatches)
            )
        if record.state is not RunState.PROVISIONAL:
            raise OrchestratorResumeError(
                f"run {record.run_id} is {record.state.value}; only provisional runs may resume"
            )

        if record.evidence_identity is None:
            return
        evidence = next(
            (
                item
                for item in record.artefacts
                if item.kind == GAMEWEEK_EVIDENCE_ARTEFACT_KIND
            ),
            None,
        )
        artifact = request.evidence_artifact
        if (
            evidence is None
            or record.evidence_identity != artifact.evidence_identity
            or evidence.reference != artifact.reference
            or evidence.sha256 != artifact.sha256
        ):
            raise OrchestratorInputError(
                f"run {record.run_id} resume evidence drift: requested manifest does not "
                "match the immutable recorded binding"
            )
        # The idempotent binding path rereads and verifies the exact persisted bytes
        # before any retry attempt is appended.
        self._records.record_evidence_manifest(
            record.run_id,
            evidence_identity=record.evidence_identity,
            artifact=artifact,
        )

    def _run_doctor_stage(self, run_id: UUID) -> bool:
        if not self._start_or_skip(run_id, DOCTOR_STAGE):
            return True
        try:
            report = self._doctor.run()
            status = self._doctor_status(report)
            self._records.finish_stage(
                run_id,
                DOCTOR_STAGE,
                status,
                note=self._doctor_summary(report),
            )
        except Exception as exc:
            self._fail_stage(run_id, DOCTOR_STAGE, exc)
            self._block_downstream(run_id, DOCTOR_STAGE)
            return False
        if status is StageState.FAIL:
            self._block_downstream(run_id, DOCTOR_STAGE)
            return False
        return True

    def _run_evidence_stage(
        self, run_id: UUID, artifact: GameweekEvidenceArtifact
    ) -> bool:
        if not self._start_or_skip(run_id, EVIDENCE_STAGE):
            return True
        try:
            self._records.record_evidence_manifest(
                run_id,
                evidence_identity=artifact.evidence_identity,
                artifact=artifact,
            )
            self._records.finish_stage(
                run_id,
                EVIDENCE_STAGE,
                StageState.PASS,
                note=f"bound evidence {artifact.evidence_identity}",
            )
        except Exception as exc:
            self._fail_stage(run_id, EVIDENCE_STAGE, exc)
            self._block_downstream(run_id, EVIDENCE_STAGE)
            return False
        return True

    def _run_baseline_stage(
        self, run_id: UUID, artifact: GameweekEvidenceArtifact
    ) -> BaselineOutcome | None:
        if not self._start_or_skip(run_id, BASELINE_STAGE):
            return None
        try:
            outcome = self._baseline.run(
                record=self._current(run_id), evidence_artifact=artifact
            )
            self._records.record_decision(
                run_id,
                reference=outcome.reference,
                sha256=outcome.sha256,
                summary=outcome.summary,
            )
            self._records.finish_stage(
                run_id,
                BASELINE_STAGE,
                StageState.PASS,
                note=outcome.summary,
            )
            return outcome
        except Exception as exc:
            self._fail_stage(run_id, BASELINE_STAGE, exc)
            return None

    def _start_or_skip(self, run_id: UUID, stage: str) -> bool:
        record = self._current(run_id)
        latest = record.latest_attempt(stage)
        if latest is None:
            self._records.start_stage(run_id, stage)
            return True
        if latest.status in (StageState.PASS, StageState.WARN):
            return False
        if latest.status in (StageState.FAIL, StageState.BLOCKED):
            self._records.retry_stage(
                run_id,
                stage,
                by=_ORCHESTRATOR_ACTOR,
                note=f"resuming {stage} after {latest.status.value}",
            )
            self._records.start_stage(run_id, stage, by=_ORCHESTRATOR_ACTOR)
            return True
        if latest.status is StageState.PENDING:
            self._records.start_stage(run_id, stage, by=latest.by, note=latest.note)
            return True
        raise OrchestratorResumeError(
            f"run {run_id} stage {stage!r} is RUNNING; #81 defines no safe stale-attempt "
            "recovery transition"
        )

    def _fail_stage(self, run_id: UUID, stage: str, exc: Exception) -> None:
        self._records.finish_stage(
            run_id,
            stage,
            StageState.FAIL,
            note=f"{type(exc).__name__}: {exc}",
        )

    def _block_downstream(self, run_id: UUID, failed_stage: str) -> None:
        failed_index = ORCHESTRATOR_STAGES.index(failed_stage)
        for stage in ORCHESTRATOR_STAGES[failed_index + 1 :]:
            record = self._current(run_id)
            latest = record.latest_attempt(stage)
            if latest is None or latest.status is StageState.PENDING:
                self._records.block_stage(
                    run_id,
                    stage,
                    note=f"blocked by {failed_stage} failure",
                )

    def _current(self, run_id: UUID) -> RunRecord:
        record = self._records.get_run(run_id)
        if isinstance(record, LegacyRunRecord):
            raise OrchestratorResumeError(f"run {run_id} unexpectedly became legacy")
        return record

    def _result(
        self, run_id: UUID, *, recommendation: DecisionRecommendation | None
    ) -> OrchestratorResult:
        record = self._current(run_id)
        if record.state is RunState.COMPLETED:
            next_action = "none; run completed"
        else:
            failed = next(
                (
                    stage
                    for stage in ORCHESTRATOR_STAGES
                    if (latest := record.latest_attempt(stage)) is not None
                    and latest.status in (StageState.FAIL, StageState.BLOCKED)
                ),
                None,
            )
            next_action = (
                f"resume run {run_id} after correcting {failed}"
                if failed is not None
                else f"inspect run {run_id} before resume"
            )
        return OrchestratorResult(
            record=record,
            recommendation=recommendation,
            next_action=next_action,
        )

    @staticmethod
    def _doctor_status(report: DoctorReport) -> StageState:
        if not report.ok:
            return StageState.FAIL
        if any(check.status is DiagnosticStatus.WARN for check in report.checks):
            return StageState.WARN
        return StageState.PASS

    @staticmethod
    def _doctor_summary(report: DoctorReport) -> str:
        failed = sorted(
            check.identifier
            for check in report.checks
            if check.status is DiagnosticStatus.FAIL
        )
        warned = sorted(
            check.identifier
            for check in report.checks
            if check.status is DiagnosticStatus.WARN
        )
        return (
            f"checks={len(report.checks)}; "
            f"failed={','.join(failed) or 'none'}; warned={','.join(warned) or 'none'}"
        )
