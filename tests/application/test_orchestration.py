"""Focused tests for the bounded resumable Gameweek orchestrator."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from fpl_decision_engine.application.doctor import (
    DiagnosticCheck,
    DiagnosticStatus,
    DoctorReport,
)
from fpl_decision_engine.application.gameweek_evidence import (
    GameweekEvidenceArtifact,
    ProjectionEvidenceInput,
    SnapshotEvidenceInput,
    build_gameweek_evidence_manifest,
    write_gameweek_evidence_manifest,
)
from fpl_decision_engine.application.orchestration import (
    BASELINE_STAGE,
    DOCTOR_STAGE,
    EVIDENCE_STAGE,
    OPERATOR_EXECUTION_CONFIRMATION_STAGE,
    ORCHESTRATOR_STAGES,
    POST_SUBMISSION_VERIFY_STAGE,
    PRE_SUBMISSION_VERIFY_STAGE,
    BaselineOutcome,
    GameweekOrchestrator,
    OrchestratorInputError,
    OrchestratorRequest,
    OrchestratorResumeError,
)
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.application.submission_safety import SUBMISSION_SAFETY_ARTEFACT_KIND
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
from fpl_decision_engine.domain.run_record import RunRecord, RunState, StageState
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
RUN_ID = UUID(int=84_001)
DEFAULT_ACQUISITION_ID = UUID(int=84_100)
NAMESPACE = UUID("12345678-1234-5678-1234-567812345678")
PLAYERS = tuple(sorted((uuid5(NAMESPACE, str(i)) for i in range(1, 16)), key=str))
ELEMENT_IDS = {player: i for i, player in enumerate(PLAYERS, start=1)}
REVERSE_ELEMENT_IDS = {value: key for key, value in ELEMENT_IDS.items()}


def recommendation() -> DecisionRecommendation:
    squad = PLAYERS
    starters = squad[:11]
    return DecisionRecommendation(
        squad_ids=squad,
        starting_xi_ids=starters,
        captain_id=starters[0],
        vice_captain_id=starters[1],
        bench_ids=squad[11:],
        formation=Formation(defenders=3, midfielders=4, forwards=3),
        squad_cost_tenths_million=1_000,
        bank_remaining_tenths_million=0,
        primary_objective=72.5,
        solver_status="Optimal",
    )


def decision_bundle() -> DecisionBundleV1:
    return DecisionBundleV1(
        decision_run_id=RUN_ID,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=NOW,
        code_revision="commit-84",
        config_fingerprint="config-84",
        inputs=DecisionInputProvenance(
            projection_provider="test",
            projection_source="test",
            projection_model_version="test",
            projection_generated_at=NOW,
        ),
        recommendation=recommendation(),
    )


def baseline_outcome() -> BaselineOutcome:
    bundle = decision_bundle()
    return BaselineOutcome(
        recommendation=bundle.recommendation,
        decision=bundle,
        reference="/state/decision-bundles/baseline.json",
        sha256="d" * 64,
        summary="objective=72.500000",
    )


def manager_snapshot(**changes: object) -> ManagerStateSnapshot:
    payload: dict[str, object] = {
        "source_provider": "test",
        "source_endpoint": "/state",
        "acquired_at_utc": NOW,
        "manager_entry_id": 42,
        "authenticated_entry_id": 42,
        "target_event_id": GameweekNumber(value=1),
        "target_deadline_time": NOW,
        "raw_picks": tuple(
            RawManagerPick(element_id=ELEMENT_IDS[player], position=position)
            for position, player in enumerate(PLAYERS, start=1)
        ),
        "squad_player_ids": tuple(ELEMENT_IDS[player] for player in PLAYERS),
        "starting_xi_player_ids": tuple(ELEMENT_IDS[player] for player in PLAYERS[:11]),
        "captain_player_id": ELEMENT_IDS[PLAYERS[0]],
        "vice_captain_player_id": ELEMENT_IDS[PLAYERS[1]],
        "reserve_goalkeeper_player_id": ELEMENT_IDS[PLAYERS[11]],
        "ordered_outfield_bench_player_ids": tuple(ELEMENT_IDS[player] for player in PLAYERS[12:]),
    }
    payload.update(changes)
    return ManagerStateSnapshot(**payload)


def verified_manager_state(**changes: object) -> ManagerStateResult:
    return ManagerStateResult(
        snapshot=manager_snapshot(**changes),
        verification=ManagerVerification.VERIFIED,
    )


def report(status: DiagnosticStatus) -> DoctorReport:
    return DoctorReport(
        (
            DiagnosticCheck(
                identifier="controlled.check",
                status=status,
                message=f"controlled {status.value}",
            ),
        )
    )


class SequenceDoctor:
    def __init__(self, *reports: DoctorReport) -> None:
        self._reports = list(reports)
        self.calls = 0

    def run(self) -> DoctorReport:
        self.calls += 1
        return self._reports.pop(0)


class SequenceBaseline:
    def __init__(self, *outcomes: BaselineOutcome | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[UUID] = []

    def run(
        self,
        *,
        record: RunRecord,
        evidence_artifact: GameweekEvidenceArtifact,
    ) -> BaselineOutcome:
        assert record.evidence_identity == evidence_artifact.evidence_identity
        self.calls.append(record.run_id)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SequenceManagerState:
    def __init__(self, *results: ManagerStateResult) -> None:
        self._results = list(results)
        self.calls: list[tuple[int, GameweekNumber]] = []

    def acquire(self, *, entry_id: int, target_event: GameweekNumber) -> ManagerStateResult:
        self.calls.append((entry_id, target_event))
        return self._results.pop(0)


def load_decision_bundle_for_test(*, reference: str, sha256: str) -> DecisionBundleV1:
    assert reference == "/state/decision-bundles/baseline.json"
    assert sha256 == "d" * 64
    return decision_bundle()


def evidence_artifact(
    tmp_path: Path,
    *,
    acquisition_id: UUID = DEFAULT_ACQUISITION_ID,
    bootstrap: bytes = b'{"bootstrap":"a"}',
    season: str = "2026-27",
    gameweek: int = 1,
) -> GameweekEvidenceArtifact:
    components = tmp_path / str(acquisition_id) / "components"
    components.mkdir(parents=True)
    bootstrap_path = components / "bootstrap-static.json"
    fixtures_path = components / "fixtures.json"
    projection_path = components / "projections.csv"
    bootstrap_path.write_bytes(bootstrap)
    fixtures_path.write_bytes(b"[]")
    projection_path.write_bytes(b"player,points\n1,5.0\n")
    manifest = build_gameweek_evidence_manifest(
        season=season,
        gameweek=GameweekNumber(value=gameweek),
        acquisition_id=acquisition_id,
        snapshot_input=SnapshotEvidenceInput(
            provider_id="fpl",
            snapshot_id=f"snapshot-{acquisition_id}",
            observed_at=NOW,
            acquired_at=NOW,
            source_reference=str(components),
            bootstrap_reference=str(bootstrap_path),
            bootstrap_content=bootstrap_path.read_bytes(),
            fixtures_reference=str(fixtures_path),
            fixtures_content=fixtures_path.read_bytes(),
        ),
        projection_input=ProjectionEvidenceInput(
            provider_id="fpl_forecast",
            source="fpl-forecast",
            generated_at=NOW,
            acquired_at=NOW,
            model_version="phase9_frontend_v1",
            artifact_reference=str(projection_path),
            artifact_content=projection_path.read_bytes(),
        ),
    )
    return write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")


def request(
    artifact: GameweekEvidenceArtifact,
    *,
    run_id: UUID = RUN_ID,
    resume: bool = False,
    operator_execution_confirmed: bool = True,
    operator: str | None = "operator-1",
) -> OrchestratorRequest:
    return OrchestratorRequest(
        run_id=run_id,
        season="2026-27",
        gameweek=1,
        code_revision="commit-84",
        config_fingerprint="config-84",
        evidence_artifact=artifact,
        expected_entry_id=42,
        player_element_ids=ELEMENT_IDS,
        element_player_ids=REVERSE_ELEMENT_IDS,
        operator=operator,
        operator_execution_confirmed=operator_execution_confirmed,
        resume=resume,
    )


def harness(
    tmp_path: Path,
    doctor: SequenceDoctor,
    baseline: SequenceBaseline,
    manager_state: SequenceManagerState | None = None,
) -> tuple[GameweekOrchestrator, RunRecordLedger]:
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    service = RunRecordService(ledger, now=lambda: NOW)
    return (
        GameweekOrchestrator(
            service,
            doctor=doctor,
            baseline=baseline,
            manager_state=manager_state
            or SequenceManagerState(verified_manager_state(), verified_manager_state()),
            state_root=tmp_path / "state",
            decision_loader=load_decision_bundle_for_test,
        ),
        ledger,
    )


def test_fresh_success_persists_one_completed_run_and_provenance(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    result = orchestrator.run(request(artifact))
    persisted = ledger.get(RUN_ID)

    assert result.record.state is RunState.COMPLETED
    assert persisted == result.record
    assert isinstance(persisted, RunRecord)
    assert persisted.evidence_identity == artifact.evidence_identity
    assert result.recommendation == baseline_outcome().recommendation
    assert result.exit_code == 0
    assert doctor.calls == 1
    assert baseline.calls == [RUN_ID]
    assert {stage: status for stage, status in result.stage_statuses} == {
        DOCTOR_STAGE: StageState.PASS,
        EVIDENCE_STAGE: StageState.PASS,
        BASELINE_STAGE: StageState.PASS,
        PRE_SUBMISSION_VERIFY_STAGE: StageState.PASS,
        OPERATOR_EXECUTION_CONFIRMATION_STAGE: StageState.PASS,
        POST_SUBMISSION_VERIFY_STAGE: StageState.PASS,
    }
    assert [
        artefact.name for artefact in persisted.artefacts
        if artefact.kind == SUBMISSION_SAFETY_ARTEFACT_KIND
    ] == [
        "pre-submission-verify-safety-attempt-1",
        "post-submission-verify-safety-attempt-1",
    ]
    assert len(tuple(ledger.root.glob("*.json"))) == 1


def test_doctor_failure_persists_fail_and_blocks_dependants(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(report(DiagnosticStatus.FAIL))
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    result = orchestrator.run(request(artifact))
    persisted = ledger.get(RUN_ID)

    assert persisted == result.record
    assert isinstance(persisted, RunRecord)
    assert persisted.state is RunState.PROVISIONAL
    assert persisted.evidence_identity is None
    assert persisted.latest_attempt(DOCTOR_STAGE).status is StageState.FAIL
    assert persisted.latest_attempt(EVIDENCE_STAGE).status is StageState.BLOCKED
    assert persisted.latest_attempt(BASELINE_STAGE).status is StageState.BLOCKED
    assert persisted.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert (
        persisted.latest_attempt(OPERATOR_EXECUTION_CONFIRMATION_STAGE).status
        is StageState.BLOCKED
    )
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert baseline.calls == []
    assert result.exit_code == 1
    assert str(RUN_ID) in result.next_action


def test_doctor_warn_is_preserved_and_permits_progression(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(report(DiagnosticStatus.WARN))
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    result = orchestrator.run(request(artifact))
    persisted = ledger.get(RUN_ID)

    assert isinstance(persisted, RunRecord)
    assert result.record.state is RunState.COMPLETED
    assert persisted.latest_attempt(DOCTOR_STAGE).status is StageState.WARN
    assert persisted.latest_attempt(EVIDENCE_STAGE).status is StageState.PASS
    assert persisted.latest_attempt(BASELINE_STAGE).status is StageState.PASS
    assert persisted.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.PASS
    assert (
        persisted.latest_attempt(OPERATOR_EXECUTION_CONFIRMATION_STAGE).status
        is StageState.PASS
    )
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.PASS


def test_evidence_failure_is_persisted_and_baseline_is_blocked(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    forged = replace(artifact, sha256="f" * 64)
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    result = orchestrator.run(request(forged))
    persisted = ledger.get(RUN_ID)

    assert isinstance(persisted, RunRecord)
    assert persisted.latest_attempt(DOCTOR_STAGE).status is StageState.PASS
    assert persisted.latest_attempt(EVIDENCE_STAGE).status is StageState.FAIL
    assert persisted.latest_attempt(BASELINE_STAGE).status is StageState.BLOCKED
    assert persisted.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert (
        persisted.latest_attempt(OPERATOR_EXECUTION_CONFIRMATION_STAGE).status
        is StageState.BLOCKED
    )
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert persisted.evidence_identity is None
    assert baseline.calls == []
    assert result.exit_code == 1


def test_baseline_exception_finishes_failed_without_recording_decision(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(RuntimeError("controlled baseline failure"))
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    result = orchestrator.run(request(artifact))
    persisted = ledger.get(RUN_ID)

    assert isinstance(persisted, RunRecord)
    assert persisted.state is RunState.PROVISIONAL
    assert persisted.latest_attempt(DOCTOR_STAGE).status is StageState.PASS
    assert persisted.latest_attempt(EVIDENCE_STAGE).status is StageState.PASS
    assert persisted.latest_attempt(BASELINE_STAGE).status is StageState.FAIL
    assert persisted.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert (
        persisted.latest_attempt(OPERATOR_EXECUTION_CONFIRMATION_STAGE).status
        is StageState.BLOCKED
    )
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert persisted.decisions == ()
    assert "RuntimeError: controlled baseline failure" in (
        persisted.latest_attempt(BASELINE_STAGE).note or ""
    )
    assert result.exit_code == 1


def test_operator_confirmation_is_explicit_and_blocks_post_verification(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    manager_state = SequenceManagerState(verified_manager_state())
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
        manager_state,
    )

    result = orchestrator.run(request(artifact, operator_execution_confirmed=False))
    persisted = ledger.get(RUN_ID)

    assert isinstance(persisted, RunRecord)
    assert result.record.state is RunState.PROVISIONAL
    assert persisted.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.PASS
    assert (
        persisted.latest_attempt(OPERATOR_EXECUTION_CONFIRMATION_STAGE).status
        is StageState.BLOCKED
    )
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.BLOCKED
    assert len(manager_state.calls) == 1


def test_resume_after_operator_confirmation_reloads_decision_and_verifies_fresh_state(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    manager_state = SequenceManagerState(verified_manager_state(), verified_manager_state())
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
        manager_state,
    )

    first = orchestrator.run(request(artifact, operator_execution_confirmed=False))
    resumed = orchestrator.run(request(artifact, resume=True))
    persisted = ledger.get(RUN_ID)

    assert first.record.state is RunState.PROVISIONAL
    assert isinstance(persisted, RunRecord)
    assert resumed.record.state is RunState.COMPLETED
    assert [
        attempt.status
        for attempt in persisted.stage_attempts
        if attempt.stage == OPERATOR_EXECUTION_CONFIRMATION_STAGE
    ] == [StageState.BLOCKED, StageState.PASS]
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.PASS
    assert manager_state.calls == [
        (42, GameweekNumber(value=1)),
        (42, GameweekNumber(value=1)),
    ]


def test_post_submission_mismatch_fails_and_records_safety_evidence(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    manager_state = SequenceManagerState(
        verified_manager_state(),
        verified_manager_state(captain_player_id=ELEMENT_IDS[PLAYERS[1]]),
    )
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
        manager_state,
    )

    result = orchestrator.run(request(artifact))
    persisted = ledger.get(RUN_ID)

    assert isinstance(persisted, RunRecord)
    assert result.record.state is RunState.PROVISIONAL
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.FAIL
    post_artefact = next(
        artefact
        for artefact in persisted.artefacts
        if artefact.name == "post-submission-verify-safety-attempt-1"
    )
    assert post_artefact.kind == SUBMISSION_SAFETY_ARTEFACT_KIND
    assert '"status":"MISMATCHED"' in Path(post_artefact.reference).read_text()


def test_post_submission_source_unavailable_fails_without_cached_fallback(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    manager_state = SequenceManagerState(
        verified_manager_state(),
        ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.SOURCE_UNAVAILABLE,
        ),
    )
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
        manager_state,
    )

    result = orchestrator.run(request(artifact))
    persisted = ledger.get(RUN_ID)

    assert isinstance(persisted, RunRecord)
    assert result.record.state is RunState.PROVISIONAL
    assert persisted.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.FAIL
    post_artefact = next(
        artefact
        for artefact in persisted.artefacts
        if artefact.name == "post-submission-verify-safety-attempt-1"
    )
    assert '"status":"SOURCE_UNAVAILABLE"' in Path(post_artefact.reference).read_text()


def test_resume_retries_failed_dependency_and_preserves_history(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(
        report(DiagnosticStatus.FAIL),
        report(DiagnosticStatus.PASS),
    )
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    first = orchestrator.run(request(artifact))
    resumed = orchestrator.run(request(artifact, resume=True))
    persisted = ledger.get(RUN_ID)

    assert first.record.state is RunState.PROVISIONAL
    assert isinstance(persisted, RunRecord)
    assert resumed.record.state is RunState.COMPLETED
    assert resumed.record.run_id == first.record.run_id
    assert [
        attempt.status
        for attempt in persisted.stage_attempts
        if attempt.stage == DOCTOR_STAGE
    ] == [StageState.FAIL, StageState.PASS]
    assert [
        attempt.status
        for attempt in persisted.stage_attempts
        if attempt.stage == EVIDENCE_STAGE
    ] == [StageState.BLOCKED, StageState.PASS]
    assert [
        attempt.status
        for attempt in persisted.stage_attempts
        if attempt.stage == BASELINE_STAGE
    ] == [StageState.BLOCKED, StageState.PASS]
    assert doctor.calls == 2
    assert baseline.calls == [RUN_ID]


def test_resume_skips_completed_doctor_and_retries_evidence(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    forged = replace(artifact, sha256="f" * 64)
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    first = orchestrator.run(request(forged))
    resumed = orchestrator.run(request(artifact, resume=True))
    persisted = ledger.get(RUN_ID)

    assert first.record.latest_attempt(EVIDENCE_STAGE).status is StageState.FAIL
    assert isinstance(persisted, RunRecord)
    assert resumed.record.state is RunState.COMPLETED
    assert doctor.calls == 1
    assert [
        attempt.status
        for attempt in persisted.stage_attempts
        if attempt.stage == EVIDENCE_STAGE
    ] == [StageState.FAIL, StageState.PASS]
    assert baseline.calls == [RUN_ID]


def _safety_artefact(record: RunRecord, name: str):
    return next(artefact for artefact in record.artefacts if artefact.name == name)


def test_completed_pre_with_missing_safety_artefact_is_not_reused(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
        SequenceManagerState(verified_manager_state()),
    )
    first = orchestrator.run(request(artifact, operator_execution_confirmed=False))
    assert first.record.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.PASS
    safety = _safety_artefact(first.record, "pre-submission-verify-safety-attempt-1")
    Path(safety.reference).unlink()

    with pytest.raises(OrchestratorResumeError, match="cannot be reused"):
        orchestrator.run(request(artifact, resume=True))

    persisted = ledger.get(RUN_ID)
    assert isinstance(persisted, RunRecord)
    assert (
        persisted.latest_attempt(OPERATOR_EXECUTION_CONFIRMATION_STAGE).status
        is StageState.BLOCKED
    )


def test_confirmation_cannot_bypass_tampered_pre_safety_evidence(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
        SequenceManagerState(verified_manager_state()),
    )
    first = orchestrator.run(request(artifact, operator_execution_confirmed=False))
    safety = _safety_artefact(first.record, "pre-submission-verify-safety-attempt-1")
    Path(safety.reference).write_text('{"schema_version":1,"kind":"wrong"}', encoding="utf-8")
    raw_before = ledger.get_raw(RUN_ID)

    with pytest.raises(OrchestratorResumeError, match="cannot be reused"):
        orchestrator.run(request(artifact, resume=True, operator_execution_confirmed=True))

    assert ledger.get_raw(RUN_ID) == raw_before


def test_completed_post_with_valid_matched_safety_artefact_is_reused(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
    )
    completed = orchestrator.run(request(artifact))
    completed_raw = ledger.get_raw(RUN_ID)
    provisional = completed.record.model_copy(
        update={"state": RunState.PROVISIONAL, "closed_at": None}
    )
    ledger.save(provisional, expected_raw=completed_raw)

    resumed = orchestrator.run(request(artifact, resume=True))

    assert resumed.record.state is RunState.COMPLETED
    assert resumed.record.latest_attempt(PRE_SUBMISSION_VERIFY_STAGE).status is StageState.PASS
    assert resumed.record.latest_attempt(POST_SUBMISSION_VERIFY_STAGE).status is StageState.PASS


def test_completed_post_with_tampered_safety_artefact_is_not_reused(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    orchestrator, ledger = harness(
        tmp_path,
        SequenceDoctor(report(DiagnosticStatus.PASS)),
        SequenceBaseline(baseline_outcome()),
    )
    completed = orchestrator.run(request(artifact))
    completed_raw = ledger.get_raw(RUN_ID)
    safety = _safety_artefact(completed.record, "post-submission-verify-safety-attempt-1")
    Path(safety.reference).write_text('{"schema_version":1,"kind":"wrong"}', encoding="utf-8")
    provisional = completed.record.model_copy(
        update={"state": RunState.PROVISIONAL, "closed_at": None}
    )
    ledger.save(provisional, expected_raw=completed_raw)
    raw_before = ledger.get_raw(RUN_ID)

    with pytest.raises(OrchestratorResumeError, match="cannot be reused"):
        orchestrator.run(request(artifact, resume=True))

    assert ledger.get_raw(RUN_ID) == raw_before


def test_resume_rejects_evidence_drift_without_mutation(tmp_path: Path) -> None:
    artifact_a = evidence_artifact(tmp_path, acquisition_id=UUID(int=84_101))
    artifact_b = evidence_artifact(
        tmp_path,
        acquisition_id=UUID(int=84_102),
        bootstrap=b'{"bootstrap":"b"}',
    )
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(RuntimeError("controlled baseline failure"))
    orchestrator, ledger = harness(tmp_path, doctor, baseline)
    first = orchestrator.run(request(artifact_a))
    raw_before = ledger.get_raw(RUN_ID)

    with pytest.raises(OrchestratorInputError, match="evidence drift"):
        orchestrator.run(
            replace(
                request(artifact_a, resume=True),
                evidence_artifact=artifact_b,
            )
        )

    assert first.record.evidence_identity == artifact_a.evidence_identity
    assert ledger.get_raw(RUN_ID) == raw_before
    assert baseline.calls == [RUN_ID]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("season", "2025-26"),
        ("gameweek", 2),
        ("code_revision", "different-commit"),
        ("config_fingerprint", "different-config"),
    ],
)
def test_resume_rejects_argument_drift_without_mutation(
    tmp_path: Path,
    field: str,
    value: str | int,
) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(RuntimeError("controlled baseline failure"))
    orchestrator, ledger = harness(tmp_path, doctor, baseline)
    orchestrator.run(request(artifact))
    raw_before = ledger.get_raw(RUN_ID)
    resumed = replace(request(artifact, resume=True), **{field: value})

    with pytest.raises(OrchestratorInputError, match="argument drift"):
        orchestrator.run(resumed)

    assert ledger.get_raw(RUN_ID) == raw_before
    assert baseline.calls == [RUN_ID]


def test_resume_rejects_running_attempt_without_guessing_staleness(
    tmp_path: Path,
) -> None:
    artifact = evidence_artifact(tmp_path)
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    service = RunRecordService(ledger, now=lambda: NOW)
    service.create_run(
        run_id=RUN_ID,
        season="2026-27",
        gameweek=1,
        mandatory_stages=ORCHESTRATOR_STAGES,
        code_revision="commit-84",
        config_fingerprint="config-84",
    )
    service.start_stage(RUN_ID, DOCTOR_STAGE)
    raw_before = ledger.get_raw(RUN_ID)
    orchestrator = GameweekOrchestrator(
        service,
        doctor=SequenceDoctor(report(DiagnosticStatus.PASS)),
        baseline=SequenceBaseline(baseline_outcome()),
        manager_state=SequenceManagerState(verified_manager_state(), verified_manager_state()),
        state_root=tmp_path / "state",
        decision_loader=load_decision_bundle_for_test,
    )

    with pytest.raises(OrchestratorResumeError, match="no safe stale-attempt"):
        orchestrator.run(request(artifact, resume=True))

    assert ledger.get_raw(RUN_ID) == raw_before

def test_resume_rejects_completed_run_without_mutation(tmp_path: Path) -> None:
    artifact = evidence_artifact(tmp_path)
    doctor = SequenceDoctor(report(DiagnosticStatus.PASS))
    baseline = SequenceBaseline(baseline_outcome())
    orchestrator, ledger = harness(tmp_path, doctor, baseline)

    completed = orchestrator.run(request(artifact))
    assert completed.record.state is RunState.COMPLETED
    raw_before = ledger.get_raw(RUN_ID)

    with pytest.raises(OrchestratorResumeError, match="only provisional"):
        orchestrator.run(request(artifact, resume=True))

    assert ledger.get_raw(RUN_ID) == raw_before
    assert baseline.calls == [RUN_ID]


def test_resume_rejects_legacy_run_without_mutation(tmp_path: Path) -> None:
    import json

    from fpl_decision_engine.domain.run_record import LegacyRunRecord

    artifact = evidence_artifact(tmp_path)
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    payload = {"run_id": str(RUN_ID), "season": "2026-27", "gameweek": 1}
    (ledger.root / f"{RUN_ID}.json").write_text(json.dumps(payload), encoding="utf-8")
    raw_before = ledger.get_raw(RUN_ID)
    assert isinstance(ledger.get(RUN_ID), LegacyRunRecord)

    service = RunRecordService(ledger, now=lambda: NOW)
    orchestrator = GameweekOrchestrator(
        service,
        doctor=SequenceDoctor(report(DiagnosticStatus.PASS)),
        baseline=SequenceBaseline(baseline_outcome()),
        manager_state=SequenceManagerState(verified_manager_state(), verified_manager_state()),
        state_root=tmp_path / "state",
        decision_loader=load_decision_bundle_for_test,
    )

    with pytest.raises(OrchestratorResumeError, match="legacy"):
        orchestrator.run(request(artifact, resume=True))

    assert ledger.get_raw(RUN_ID) == raw_before
