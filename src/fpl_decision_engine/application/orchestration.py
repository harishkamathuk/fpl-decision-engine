"""Resumable orchestration for the bounded Touchline Gameweek workflow.

The first orchestrator deliberately owns only stage control. Doctor diagnostics,
immutable evidence verification and baseline optimisation remain behind their existing
application seams; stage attempts and results are persisted exclusively through the
typed RunRecord ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fpl_decision_engine.application.doctor import (
    DiagnosticStatus,
    DoctorReport,
)
from fpl_decision_engine.application.gameweek_evidence import GameweekEvidenceArtifact
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.application.submission_safety import (
    SUBMISSION_SAFETY_ARTEFACT_KIND,
    SafetyStatus,
    SubmissionSafetyArtifactError,
    SubmissionSafetyResult,
    load_submission_safety_result,
    plan_submission,
    verify_submission,
    write_submission_safety_result,
)
from fpl_decision_engine.domain.decision_bundle import DecisionBundleV1, DecisionRecommendation
from fpl_decision_engine.domain.manager_state import ManagerStateResult, ManagerStateSnapshot
from fpl_decision_engine.domain.run_record import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    CloseOutcome,
    LegacyRunRecord,
    RunRecord,
    RunState,
    StageState,
)
from fpl_decision_engine.domain.value_objects import GameweekNumber

DOCTOR_STAGE = "doctor"
EVIDENCE_STAGE = "evidence"
BASELINE_STAGE = "baseline"
PRE_SUBMISSION_VERIFY_STAGE = "pre-submission-verify"
OPERATOR_EXECUTION_CONFIRMATION_STAGE = "operator-execution-confirmation"
POST_SUBMISSION_VERIFY_STAGE = "post-submission-verify"
ORCHESTRATOR_STAGES = (
    DOCTOR_STAGE,
    EVIDENCE_STAGE,
    BASELINE_STAGE,
    PRE_SUBMISSION_VERIFY_STAGE,
    OPERATOR_EXECUTION_CONFIRMATION_STAGE,
    POST_SUBMISSION_VERIFY_STAGE,
)
_ORCHESTRATOR_ACTOR = "touchline-orchestrator"


class DoctorRunner(Protocol):
    """Run the existing deterministic doctor seam."""

    def run(self) -> DoctorReport: ...


class ManagerStateRunner(Protocol):
    """Acquire one fresh verified #87 manager-state result for submission safety."""

    def acquire(self, *, entry_id: int, target_event: GameweekNumber) -> ManagerStateResult: ...


@dataclass(frozen=True, slots=True)
class BaselineOutcome:
    """Semantic recommendation and immutable artifact produced by the baseline seam."""

    recommendation: DecisionRecommendation
    decision: DecisionBundleV1
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
    expected_entry_id: int | None = None
    player_element_ids: Mapping[UUID, int] | None = None
    element_player_ids: Mapping[int, UUID] | None = None
    previous_verified_manager_state: ManagerStateSnapshot | None = None
    previous_state_acknowledged: bool = False
    operator: str | None = None
    operator_execution_confirmed: bool = False
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
        manager_state: ManagerStateRunner,
        state_root: Path,
        decision_loader: Callable[..., DecisionBundleV1],
    ) -> None:
        self._records = records
        self._doctor = doctor
        self._baseline = baseline
        self._manager_state = manager_state
        self._state_root = state_root
        self._decision_loader = decision_loader

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
        decision = self._decision_for_submission(record.run_id, outcome)

        if not self._run_pre_submission_verify_stage(record.run_id, request, decision):
            return self._result(record.run_id, recommendation=None)
        if not self._run_operator_execution_confirmation_stage(record.run_id, request):
            return self._result(record.run_id, recommendation=None)
        if not self._run_post_submission_verify_stage(record.run_id, request, decision):
            return self._result(record.run_id, recommendation=None)

        if record.state is RunState.PROVISIONAL:
            self._records.close_run(record.run_id, outcome=CloseOutcome.COMPLETED)
        return self._result(record.run_id, recommendation=decision.recommendation)

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
            self._block_downstream(run_id, BASELINE_STAGE)
            return None

    def _run_pre_submission_verify_stage(
        self, run_id: UUID, request: OrchestratorRequest, decision: DecisionBundleV1
    ) -> bool:
        if not self._prepare_submission_stage(
            run_id, PRE_SUBMISSION_VERIFY_STAGE, decision=decision
        ):
            return True
        try:
            entry_id = self._expected_entry_id(request)
            current = self._manager_state.acquire(
                entry_id=entry_id,
                target_event=GameweekNumber(value=request.gameweek),
            )
            result = plan_submission(
                current,
                decision,
                expected_entry_id=entry_id,
                expected_gameweek=request.gameweek,
                previous_verified=request.previous_verified_manager_state,
                previous_acknowledged=request.previous_state_acknowledged,
                player_element_ids=request.player_element_ids,
            )
            self._record_submission_safety_result(
                run_id, stage=PRE_SUBMISSION_VERIFY_STAGE, result=result
            )
            if result.blocking:
                self._records.block_stage(
                    run_id,
                    PRE_SUBMISSION_VERIFY_STAGE,
                    note=self._safety_summary(result),
                )
                self._block_downstream(run_id, PRE_SUBMISSION_VERIFY_STAGE)
                return False
            self._records.start_stage(run_id, PRE_SUBMISSION_VERIFY_STAGE)
            self._records.finish_stage(
                run_id,
                PRE_SUBMISSION_VERIFY_STAGE,
                StageState.PASS,
                note=self._safety_summary(result),
            )
            return True
        except Exception as exc:
            self._fail_submission_stage(run_id, PRE_SUBMISSION_VERIFY_STAGE, exc)
            return False

    def _run_operator_execution_confirmation_stage(
        self, run_id: UUID, request: OrchestratorRequest
    ) -> bool:
        if not self._prepare_submission_stage(
            run_id, OPERATOR_EXECUTION_CONFIRMATION_STAGE
        ):
            return True
        if not request.operator_execution_confirmed:
            self._records.block_stage(
                run_id,
                OPERATOR_EXECUTION_CONFIRMATION_STAGE,
                by=request.operator,
                note="operator execution confirmation required",
            )
            self._block_downstream(run_id, OPERATOR_EXECUTION_CONFIRMATION_STAGE)
            return False
        if not request.operator or not request.operator.strip():
            self._records.block_stage(
                run_id,
                OPERATOR_EXECUTION_CONFIRMATION_STAGE,
                note="operator attribution is required",
            )
            self._block_downstream(run_id, OPERATOR_EXECUTION_CONFIRMATION_STAGE)
            return False
        self._records.start_stage(
            run_id, OPERATOR_EXECUTION_CONFIRMATION_STAGE, by=request.operator.strip()
        )
        self._records.finish_stage(
            run_id,
            OPERATOR_EXECUTION_CONFIRMATION_STAGE,
            StageState.PASS,
            by=request.operator.strip(),
            note="operator reports external FPL action attempted/completed",
        )
        return True

    def _run_post_submission_verify_stage(
        self, run_id: UUID, request: OrchestratorRequest, decision: DecisionBundleV1
    ) -> bool:
        if not self._prepare_submission_stage(
            run_id, POST_SUBMISSION_VERIFY_STAGE, decision=decision
        ):
            return True
        try:
            entry_id = self._expected_entry_id(request)
            observed = self._manager_state.acquire(
                entry_id=entry_id,
                target_event=GameweekNumber(value=request.gameweek),
            )
            result = verify_submission(
                decision,
                observed,
                player_element_ids=request.player_element_ids,
                element_player_ids=request.element_player_ids,
            )
            self._record_submission_safety_result(
                run_id, stage=POST_SUBMISSION_VERIFY_STAGE, result=result
            )
            self._records.start_stage(run_id, POST_SUBMISSION_VERIFY_STAGE)
            self._records.finish_stage(
                run_id,
                POST_SUBMISSION_VERIFY_STAGE,
                StageState.PASS if result.status is SafetyStatus.MATCHED else StageState.FAIL,
                note=self._safety_summary(result),
            )
            return result.status is SafetyStatus.MATCHED
        except Exception as exc:
            self._fail_submission_stage(run_id, POST_SUBMISSION_VERIFY_STAGE, exc)
            return False

    def _decision_for_submission(
        self, run_id: UUID, outcome: BaselineOutcome | None
    ) -> DecisionBundleV1:
        if outcome is not None:
            return outcome.decision
        record = self._current(run_id)
        if not record.decisions:
            raise OrchestratorResumeError(
                f"run {run_id} has no recorded decision for submission verification"
            )
        recorded = record.decisions[-1]
        return self._decision_loader(reference=recorded.reference, sha256=recorded.sha256)

    def _prepare_submission_stage(
        self,
        run_id: UUID,
        stage: str,
        *,
        decision: DecisionBundleV1 | None = None,
    ) -> bool:
        record = self._current(run_id)
        latest = record.latest_attempt(stage)
        if latest is None:
            return True
        if latest.status in (StageState.PASS, StageState.WARN):
            self._validate_reusable_submission_stage(
                run_id, stage, latest.attempt, decision=decision
            )
            return False
        if latest.status in (StageState.FAIL, StageState.BLOCKED):
            self._records.retry_stage(
                run_id,
                stage,
                by=_ORCHESTRATOR_ACTOR,
                note=f"resuming {stage} after {latest.status.value}",
            )
            return True
        if latest.status is StageState.PENDING:
            return True
        raise OrchestratorResumeError(
            f"run {run_id} stage {stage!r} is RUNNING; #81 defines no safe stale-attempt "
            "recovery transition"
        )

    def _validate_reusable_submission_stage(
        self,
        run_id: UUID,
        stage: str,
        attempt: int,
        *,
        decision: DecisionBundleV1 | None,
    ) -> None:
        if stage not in (PRE_SUBMISSION_VERIFY_STAGE, POST_SUBMISSION_VERIFY_STAGE):
            return
        record = self._current(run_id)
        name = f"{stage}-safety-attempt-{attempt}"
        artefact = next((item for item in record.artefacts if item.name == name), None)
        if artefact is None:
            raise OrchestratorResumeError(
                f"run {run_id} stage {stage!r} cannot be reused: "
                f"missing safety artefact {name!r}"
            )
        if artefact.kind != SUBMISSION_SAFETY_ARTEFACT_KIND:
            raise OrchestratorResumeError(
                f"run {run_id} stage {stage!r} cannot be reused: "
                f"unexpected safety artefact kind {artefact.kind!r}"
            )
        expected_phase = (
            "PRE_EXECUTION" if stage == PRE_SUBMISSION_VERIFY_STAGE else "POST_EXECUTION"
        )
        if decision is None:
            raise OrchestratorResumeError(
                f"run {run_id} stage {stage!r} cannot be reused without the exact "
                "recorded DecisionBundle"
            )
        try:
            result = load_submission_safety_result(
                reference=artefact.reference,
                sha256=artefact.sha256,
                expected_phase=expected_phase,
                expected_decision=decision,
            )
        except SubmissionSafetyArtifactError as exc:
            raise OrchestratorResumeError(
                f"run {run_id} stage {stage!r} cannot be reused: {exc}"
            ) from exc
        expected_status = (
            SafetyStatus.SAFE if stage == PRE_SUBMISSION_VERIFY_STAGE else SafetyStatus.MATCHED
        )
        if result.status is not expected_status or result.blocking:
            raise OrchestratorResumeError(
                f"run {run_id} stage {stage!r} cannot be reused: "
                f"safety result is {result.status.value}"
            )

    def _record_submission_safety_result(
        self, run_id: UUID, *, stage: str, result: SubmissionSafetyResult
    ) -> None:
        record = self._current(run_id)
        latest = record.latest_attempt(stage)
        attempt = latest.attempt if latest is not None else 1
        artifact = write_submission_safety_result(result, state_root=self._state_root)
        self._records.record_artefact(
            run_id,
            name=f"{stage}-safety-attempt-{attempt}",
            reference=artifact.reference,
            sha256=artifact.sha256,
            kind=SUBMISSION_SAFETY_ARTEFACT_KIND,
        )

    def _fail_submission_stage(self, run_id: UUID, stage: str, exc: Exception) -> None:
        self._records.start_stage(run_id, stage)
        self._fail_stage(run_id, stage, exc)
        self._block_downstream(run_id, stage)

    @staticmethod
    def _expected_entry_id(request: OrchestratorRequest) -> int:
        if request.expected_entry_id is None:
            raise OrchestratorInputError("expected_entry_id is required for submission safety")
        return request.expected_entry_id

    @staticmethod
    def _safety_summary(result: SubmissionSafetyResult) -> str:
        details = ",".join(result.details) if result.details else "none"
        return (
            f"{result.phase} {result.status.value}; "
            f"blocking={result.blocking}; details={details}"
        )

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
