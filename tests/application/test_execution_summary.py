from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fpl_decision_engine.application.decision_bundles import serialize_decision_bundle
from fpl_decision_engine.application.execution_summary import (
    NOT_RECORDED,
    SafetyAuthorityState,
    build_execution_summary,
    render_execution_summary_json,
    render_execution_summary_text,
)
from fpl_decision_engine.application.gameweek_evidence import (
    ProjectionEvidenceInput,
    SnapshotEvidenceInput,
    build_gameweek_evidence_manifest,
    serialize_gameweek_evidence_manifest,
)
from fpl_decision_engine.application.submission_safety import (
    SUBMISSION_SAFETY_ARTEFACT_KIND,
    SUBMISSION_SAFETY_ARTEFACT_KIND_V1,
    SafetyStatus,
    SubmissionSafetyResult,
    serialize_submission_safety_result,
)
from fpl_decision_engine.domain.decision_bundle import (
    DecisionBundleV1,
    DecisionInputProvenance,
    DecisionRecommendation,
)
from fpl_decision_engine.domain.optimisation import Formation
from fpl_decision_engine.domain.provenance import DecisionProvenance
from fpl_decision_engine.domain.run_record import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    LegacyRunRecord,
    RecordedDecision,
    RunArtefact,
    RunRecord,
    RunState,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.domain.value_objects import GameweekNumber

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
EVIDENCE_IDENTITY = f"sha256:{'e' * 64}"


def attempt(stage: str, number: int, status: StageState, **overrides: object) -> StageAttempt:
    values: dict[str, object] = {
        "stage": stage,
        "attempt": number,
        "status": status,
        "started_at": NOW,
        "finished_at": NOW + timedelta(seconds=12),
    }
    values.update(overrides)
    return StageAttempt.model_validate(values)


def record(**overrides: object) -> RunRecord:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "season": "2026-27",
        "gameweek": 1,
        "created_at": NOW,
        "mandatory_stages": ("ingest", "optimise"),
    }
    values.update(overrides)
    return RunRecord.model_validate(values)


def make_bundle(run_id: UUID) -> DecisionBundleV1:
    ids = tuple(uuid4() for _ in range(15))
    return DecisionBundleV1(
        decision_run_id=run_id,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=NOW,
        code_revision="revision",
        config_fingerprint="config",
        inputs=DecisionInputProvenance(
            projection_provider="provider",
            projection_source="source",
            projection_model_version="model",
            projection_generated_at=NOW,
        ),
        recommendation=DecisionRecommendation(
            squad_ids=tuple(sorted(ids, key=str)),
            starting_xi_ids=tuple(sorted(ids[:11], key=str)),
            captain_id=ids[0],
            vice_captain_id=ids[1],
            bench_ids=ids[11:],
            formation=Formation(defenders=4, midfielders=4, forwards=2),
            squad_cost_tenths_million=1000,
            bank_remaining_tenths_million=0,
            primary_objective=12.5,
            solver_status="optimal",
        ),
    )


def evidence_stub(tmp_path: Path) -> RunArtefact:
    path = tmp_path / "evidence.json"
    path.write_text("{}", encoding="utf-8")
    return RunArtefact(
        name="gameweek-evidence",
        reference=str(path),
        sha256="a" * 64,
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
        recorded_at=NOW,
    )


def decision_record(run_id: UUID, path: Path, bundle: DecisionBundleV1) -> RecordedDecision:
    content_hash = hashlib.sha256(serialize_decision_bundle(bundle)).hexdigest()
    return RecordedDecision(
        reference=str(path),
        provenance=DecisionProvenance(
            run_id=run_id,
            decision_run_id=bundle.decision_run_id,
            evidence_identity=EVIDENCE_IDENTITY,
            decision_artifact_hash=content_hash,
        ),
        recorded_at=NOW,
    )


def safety_artefact(
    tmp_path: Path,
    bundle: DecisionBundleV1,
    *,
    kind: str = SUBMISSION_SAFETY_ARTEFACT_KIND,
    include_binding: bool = True,
) -> RunArtefact:
    result = SubmissionSafetyResult(
        phase="POST_EXECUTION",
        status=SafetyStatus.SOURCE_UNAVAILABLE,
        blocking=True,
        decision_run_id=bundle.decision_run_id if include_binding else None,
        decision_identity=(
            hashlib.sha256(serialize_decision_bundle(bundle)).hexdigest()
            if include_binding
            else None
        ),
    )
    content = serialize_submission_safety_result(result)
    if kind == SUBMISSION_SAFETY_ARTEFACT_KIND_V1:
        content = content.replace(b'"schema_version":2', b'"schema_version":1')
        content = content.replace(
            b'"kind":"submission-safety-result-v2"',
            b'"kind":"submission-safety-result-v1"',
        )
    path = tmp_path / f"{kind}.json"
    path.write_bytes(content)
    return RunArtefact(
        name=kind,
        reference=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        kind=kind,
        recorded_at=NOW,
    )


def test_completed_summary_contains_run_and_exact_run_level_artefacts() -> None:
    artefact = RunArtefact(
        name="custom-output",
        reference="state/custom.json",
        sha256="a" * 64,
        kind="custom-kind",
        recorded_at=NOW,
    )
    current = record(
        state=RunState.COMPLETED,
        closed_at=NOW + timedelta(minutes=1),
        stage_attempts=(
            attempt("ingest", 1, StageState.PASS),
            attempt("optimise", 1, StageState.WARN),
        ),
        artefacts=(artefact,),
    )
    summary = build_execution_summary(current)
    assert summary.run["run_id"] == str(current.run_id)
    assert summary.run["trigger"] == NOT_RECORDED
    assert summary.run["scenario_status"] == NOT_RECORDED
    assert summary.artefacts[0].to_dict()["reference"] == "state/custom.json"
    assert all("artefact" not in stage.to_dict() for stage in summary.stages)


def test_failed_provisional_and_partial_records_are_read_only_views() -> None:
    failed = record(
        state=RunState.FAILED,
        closed_at=NOW,
        stage_attempts=(attempt("ingest", 1, StageState.FAIL),),
    )
    provisional = record(
        stage_attempts=(attempt("ingest", 1, StageState.RUNNING, finished_at=None),)
    )
    partial = record(stage_attempts=())
    assert build_execution_summary(failed).run["state"] == "failed"
    assert build_execution_summary(provisional).stages[0].attempts[0].duration_seconds is None
    assert build_execution_summary(partial).stages[1].latest_status == NOT_RECORDED


def test_legacy_record_does_not_crash_or_fabricate() -> None:
    summary = build_execution_summary(
        LegacyRunRecord(run_id=uuid4(), season="2026-27", gameweek=1, raw={"status": "done"})
    )
    assert summary.run["trigger"] == NOT_RECORDED
    assert summary.run["state"] == NOT_RECORDED
    assert summary.stages == ()
    assert any(item.kind.value == "legacy" for item in summary.warnings)


def test_all_terminal_duration_rules_and_retries_are_preserved() -> None:
    attempts = (
        attempt("custom", 1, StageState.BLOCKED, started_at=None),
        attempt("stage", 1, StageState.FAIL),
        attempt(
            "stage",
            2,
            StageState.WARN,
            started_at=NOW + timedelta(minutes=1),
            finished_at=NOW + timedelta(minutes=2),
        ),
        attempt(
            "stage",
            3,
            StageState.PASS,
            started_at=NOW + timedelta(minutes=3),
            finished_at=NOW + timedelta(minutes=4),
        ),
    )
    summary = build_execution_summary(record(mandatory_stages=("stage",), stage_attempts=attempts))
    stage = next(item for item in summary.stages if item.stage == "stage")
    assert [item.status for item in stage.attempts] == ["fail", "warn", "pass"]
    assert [item.duration_seconds for item in stage.attempts] == [12.0, 60.0, 60.0]
    assert (
        next(item for item in summary.stages if item.stage == "custom")
        .attempts[0]
        .duration_seconds
        is None
    )


def test_json_and_text_are_deterministic() -> None:
    current = record(stage_attempts=(attempt("ingest", 1, StageState.PASS),))
    first = build_execution_summary(current)
    second = build_execution_summary(current)
    assert render_execution_summary_json(first) == render_execution_summary_json(second)
    assert render_execution_summary_text(first) == render_execution_summary_text(second)
    parsed = json.loads(render_execution_summary_json(first))
    assert parsed["comparison_status"] == NOT_RECORDED


def test_explicit_evidence_manifest_failure_is_warning_not_guess() -> None:
    evidence = RunArtefact(
        name="gameweek-evidence",
        reference="missing-manifest.json",
        sha256="a" * 64,
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
        recorded_at=NOW,
    )
    summary = build_execution_summary(
        record(evidence_identity=EVIDENCE_IDENTITY, artefacts=(evidence,))
    )
    assert summary.evidence["provider"] == NOT_RECORDED
    assert any("evidence manifest" in item.message for item in summary.warnings)


def test_evidence_enrichment_and_tamper_warning() -> None:
    bootstrap = b'{"elements":[{"id":1}],"events":[],"teams":[]}'
    fixtures = b'[{"id":1,"event":1}]'
    projection = b"player_id,gameweek,expected_points\n1,1,5.2\n"
    manifest = build_gameweek_evidence_manifest(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        acquisition_id=uuid4(),
        snapshot_input=SnapshotEvidenceInput(
            provider_id="fpl",
            snapshot_id="snapshot-a",
            observed_at=NOW,
            acquired_at=NOW,
            source_reference="manifest.json",
            bootstrap_reference="bootstrap.json",
            bootstrap_content=bootstrap,
            fixtures_reference="fixtures.json",
            fixtures_content=fixtures,
        ),
        projection_input=ProjectionEvidenceInput(
            provider_id="forecast", source="forecast", generated_at=NOW, acquired_at=NOW,
            model_version="model", artifact_reference="projection.csv", artifact_content=projection,
        ),
    )
    manifest_content = serialize_gameweek_evidence_manifest(manifest)
    refs = {
        "manifest.json": manifest_content,
        "bootstrap.json": bootstrap,
        "fixtures.json": fixtures,
        "projection.csv": projection,
    }
    evidence = RunArtefact(
        name="gameweek-evidence", reference="manifest.json",
        sha256=hashlib.sha256(manifest_content).hexdigest(),
        kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND, recorded_at=NOW,
    )
    summary = build_execution_summary(
        record(evidence_identity=manifest.evidence_identity, artefacts=(evidence,)),
        evidence_loader=refs.__getitem__,
    )
    assert summary.evidence["provider"] == "fpl"
    tampered = dict(refs, **{"manifest.json": manifest_content + b"tampered"})
    summary = build_execution_summary(
        record(evidence_identity=manifest.evidence_identity, artefacts=(evidence,)),
        evidence_loader=tampered.__getitem__,
    )
    assert summary.evidence["provider"] == NOT_RECORDED
    assert summary.warnings


def test_v2_exact_decision_is_proven(tmp_path: Path) -> None:
    run_id = uuid4()
    bundle = make_bundle(run_id)
    decision_path = tmp_path / "decision.json"
    decision_bytes = serialize_decision_bundle(bundle)
    decision_path.write_bytes(decision_bytes)
    decision = decision_record(run_id, decision_path, bundle)
    safety = safety_artefact(tmp_path, bundle)
    summary = build_execution_summary(
        record(
            run_id=run_id,
            evidence_identity=EVIDENCE_IDENTITY,
            decisions=(decision,),
            artefacts=(evidence_stub(tmp_path), safety),
        )
    )
    assert (
        summary.submission_safety[0]["authoritative_binding"]
        == SafetyAuthorityState.PROVEN.value
    )


def test_v2_wrong_decision_is_not_proven_with_warning(tmp_path: Path) -> None:
    run_id = uuid4()
    expected = make_bundle(run_id)
    wrong = make_bundle(uuid4())
    decision_path = tmp_path / "decision.json"
    decision_bytes = serialize_decision_bundle(wrong)
    decision_path.write_bytes(decision_bytes)
    decision = decision_record(run_id, decision_path, wrong)
    safety = safety_artefact(tmp_path, expected)
    summary = build_execution_summary(
        record(
            run_id=run_id,
            evidence_identity=EVIDENCE_IDENTITY,
            decisions=(decision,),
            artefacts=(evidence_stub(tmp_path), safety),
        )
    )
    assert (
        summary.submission_safety[0]["authoritative_binding"]
        == SafetyAuthorityState.NOT_PROVEN.value
    )
    assert any("submission safety" in warning.message for warning in summary.warnings)


def test_v2_missing_decision_is_not_proven_with_warning(tmp_path: Path) -> None:
    safety = safety_artefact(tmp_path, make_bundle(uuid4()))
    summary = build_execution_summary(
        record(evidence_identity=EVIDENCE_IDENTITY, artefacts=(evidence_stub(tmp_path), safety))
    )
    assert (
        summary.submission_safety[0]["authoritative_binding"]
        == SafetyAuthorityState.NOT_PROVEN.value
    )
    assert any("could not be proven" in warning.message for warning in summary.warnings)


def test_v2_tampered_decision_identity_is_not_proven(tmp_path: Path) -> None:
    run_id = uuid4()
    bundle = make_bundle(run_id)
    safety = safety_artefact(tmp_path, bundle)
    path = Path(safety.reference)
    content = path.read_bytes().replace(
        hashlib.sha256(serialize_decision_bundle(bundle)).hexdigest().encode(),
        b"0" * 64,
    )
    path.write_bytes(content)
    summary = build_execution_summary(
        record(evidence_identity=EVIDENCE_IDENTITY, artefacts=(evidence_stub(tmp_path), safety))
    )
    assert (
        summary.submission_safety[0]["authoritative_binding"]
        == SafetyAuthorityState.NOT_PROVEN.value
    )
    assert summary.warnings


def test_v2_tampered_content_hash_is_not_proven(tmp_path: Path) -> None:
    bundle = make_bundle(uuid4())
    safety = safety_artefact(tmp_path, bundle)
    path = Path(safety.reference)
    path.write_bytes(path.read_bytes() + b"tampered")
    summary = build_execution_summary(
        record(evidence_identity=EVIDENCE_IDENTITY, artefacts=(evidence_stub(tmp_path), safety))
    )
    assert (
        summary.submission_safety[0]["authoritative_binding"]
        == SafetyAuthorityState.NOT_PROVEN.value
    )
    assert summary.warnings


def test_v1_is_historical_and_non_authoritative(tmp_path: Path) -> None:
    bundle = make_bundle(uuid4())
    safety = safety_artefact(
        tmp_path,
        bundle,
        kind=SUBMISSION_SAFETY_ARTEFACT_KIND_V1,
        include_binding=False,
    )
    summary = build_execution_summary(
        record(evidence_identity=EVIDENCE_IDENTITY, artefacts=(evidence_stub(tmp_path), safety))
    )
    assert summary.submission_safety[0]["historical"] is True
    assert (
        summary.submission_safety[0]["authoritative_binding"]
        == SafetyAuthorityState.NON_AUTHORITATIVE.value
    )


def test_human_renderer_covers_operator_fields(tmp_path: Path) -> None:
    run_id = uuid4()
    bundle = make_bundle(run_id)
    decision_path = tmp_path / "decision.json"
    decision_bytes = serialize_decision_bundle(bundle)
    decision_path.write_bytes(decision_bytes)
    decision = decision_record(run_id, decision_path, bundle)
    safety = safety_artefact(tmp_path, bundle)
    current = record(
        run_id=run_id,
        evidence_identity=EVIDENCE_IDENTITY,
        code_revision="revision",
        config_fingerprint="config",
        diagnostic_summary="diagnostic warning",
        decisions=(decision,),
        artefacts=(evidence_stub(tmp_path), safety),
        stage_attempts=(attempt("ingest", 1, StageState.WARN, by="operator", note="reviewed"),),
    )
    text = render_execution_summary_text(build_execution_summary(current))
    for field in (
        "revision",
        "config",
        "comparison_status",
        "ingest",
        "warn",
        "duration=",
        "decision_run_id",
        "submission_safety",
        "PROVEN",
        "authority:",
        "warnings:",
    ):
        assert field in text


def test_record_is_not_mutated() -> None:
    current = record(stage_attempts=(attempt("ingest", 1, StageState.PASS),))
    before = current.model_dump_json()
    build_execution_summary(current)
    assert current.model_dump_json() == before
