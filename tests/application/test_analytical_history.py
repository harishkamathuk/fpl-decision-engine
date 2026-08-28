"""Issue #85 operational integration tests: downstream history/comparison artefacts.

Covers the five approved cases: no previous run, valid previous run, invalid/missing
previous state, changed recommendation and unchanged recommendation (including
differing artefact hashes with equal recommendation identity), plus the immutable
decision-bundle read seam and the completed-run execution boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.application import (
    AnalyticalArtifactService,
    AnalyticalHistoryError,
    AnalyticalHistoryService,
    DecisionBundleError,
    RecommendationChange,
    load_decision_bundle,
    parse_decision_bundle,
    serialize_decision_bundle,
)
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    AnalyticalArtifact,
    AnalyticalArtifactType,
    DecisionBundleV1,
    DecisionInputProvenance,
    DecisionProvenance,
    DecisionRecommendation,
    Formation,
    GameweekNumber,
    RecordedDecision,
    RunArtefact,
    RunRecord,
    RunState,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger
from fpl_decision_engine.ports import PersistedAnalyticalArtifact

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


class MemoryRepository:
    """Content-addressed in-memory analytical artefact repository for tests."""

    def __init__(self) -> None:
        self.artifacts: dict[str, AnalyticalArtifact] = {}

    def publish(self, artifact: AnalyticalArtifact) -> PersistedAnalyticalArtifact:
        stored = self.artifacts.setdefault(artifact.analysis_artifact_id, artifact)
        return PersistedAnalyticalArtifact(
            analysis_artifact_id=stored.analysis_artifact_id,
            reference=f"memory:{stored.analysis_artifact_id}",
            sha256="a" * 64,
        )

    def load(self, analysis_artifact_id: str) -> AnalyticalArtifact | None:
        return self.artifacts.get(analysis_artifact_id)


def _setup(
    tmp_path: Path,
) -> tuple[AnalyticalHistoryService, RunRecordService, MemoryRepository, RunRecordLedger]:
    ledger = RunRecordLedger(tmp_path / "run-records")
    records = RunRecordService(ledger, now=lambda: NOW)
    repository = MemoryRepository()
    service = AnalyticalHistoryService(
        records=records,
        bundle_loader=load_decision_bundle,
        analytical=AnalyticalArtifactService(repository),
        now=lambda: NOW,
    )
    return service, records, repository, ledger


def _squad_ids(offset: int = 0) -> tuple[UUID, ...]:
    return tuple(sorted((UUID(int=1000 + offset + index) for index in range(15)), key=str))


def _recommendation(*, offset: int = 0, captain: int = 0) -> DecisionRecommendation:
    squad = _squad_ids(offset)
    starters = squad[:11]
    bench = squad[11:]
    return DecisionRecommendation(
        squad_ids=squad,
        starting_xi_ids=starters,
        captain_id=starters[captain],
        vice_captain_id=starters[1] if captain != 1 else starters[0],
        bench_ids=bench,
        formation=Formation(defenders=3, midfielders=5, forwards=2),
        squad_cost_tenths_million=500,
        bank_remaining_tenths_million=20,
        primary_objective=75.5,
        solver_status="optimal",
    )


def _bundle(
    *,
    decision_run_id: UUID,
    recommendation: DecisionRecommendation,
    config_fingerprint: str = "config-1",
) -> DecisionBundleV1:
    return DecisionBundleV1(
        decision_run_id=decision_run_id,
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        decision_at=NOW,
        code_revision="commit-85",
        config_fingerprint=config_fingerprint,
        inputs=DecisionInputProvenance(
            projection_provider="fpl_forecast",
            projection_source="fpl-forecast",
            projection_model_version="phase9_frontend_v1",
            projection_generated_at=NOW - timedelta(hours=1),
        ),
        recommendation=recommendation,
    )


def _persist_bundle(bundle: DecisionBundleV1, tmp_path: Path) -> tuple[str, str]:
    content = serialize_decision_bundle(bundle)
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / f"{digest}.json"
    path.write_bytes(content)
    return str(path), digest


def _completed_run(
    *,
    run_id: UUID,
    decision_run_id: UUID,
    evidence_identity: str,
    decision_reference: str,
    decision_hash: str,
    previous_run_id: UUID | None = None,
    state: RunState = RunState.COMPLETED,
) -> RunRecord:
    stage_attempts = (
        StageAttempt(
            stage="baseline",
            attempt=1,
            status=StageState.PASS,
            started_at=NOW - timedelta(minutes=1),
            finished_at=NOW,
        ),
    )
    decision = RecordedDecision(
        reference=decision_reference,
        provenance=DecisionProvenance(
            run_id=run_id,
            decision_run_id=decision_run_id,
            evidence_identity=evidence_identity,
            decision_artifact_hash=decision_hash,
        ),
        recorded_at=NOW,
    )
    return RunRecord(
        run_id=run_id,
        season="2026-27",
        gameweek=1,
        created_at=NOW - timedelta(hours=2),
        previous_run_id=previous_run_id,
        mandatory_stages=("baseline",),
        state=state,
        stage_attempts=stage_attempts if state is not RunState.PROVISIONAL else (),
        artefacts=(
            RunArtefact(
                name="gameweek-evidence",
                reference="state/evidence.json",
                sha256="b" * 64,
                kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
                recorded_at=NOW,
            ),
        ),
        decisions=(decision,),
        closed_at=NOW if state is not RunState.PROVISIONAL else None,
        code_revision="commit-85",
        config_fingerprint="config-1",
        evidence_identity=evidence_identity,
    )


def _identity_content(recommendation: DecisionRecommendation) -> dict[str, object]:
    return {
        "squad_ids": [str(value) for value in recommendation.squad_ids],
        "starting_xi_ids": [str(value) for value in recommendation.starting_xi_ids],
        "captain_id": str(recommendation.captain_id),
        "vice_captain_id": str(recommendation.vice_captain_id),
        "bench_ids": [str(value) for value in recommendation.bench_ids],
    }


def _seed_run(
    ledger: RunRecordLedger,
    *,
    run_id: UUID,
    decision_run_id: UUID,
    evidence_identity: str,
    bundle: DecisionBundleV1,
    tmp_path: Path,
    previous_run_id: UUID | None = None,
) -> tuple[str, str]:
    reference, digest = _persist_bundle(bundle, tmp_path)
    ledger.save(
        _completed_run(
            run_id=run_id,
            decision_run_id=decision_run_id,
            evidence_identity=evidence_identity,
            decision_reference=reference,
            decision_hash=digest,
            previous_run_id=previous_run_id,
        )
    )
    return reference, digest


# --- Case 1: no previous run -------------------------------------------------


def test_no_previous_run_preserves_no_previous_outcome(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    run_id = UUID(int=85_101)
    decision_run_id = UUID(int=85_102)
    evidence = f"sha256:{'e' * 64}"
    bundle = _bundle(decision_run_id=decision_run_id, recommendation=_recommendation())
    _, digest = _seed_run(
        ledger,
        run_id=run_id,
        decision_run_id=decision_run_id,
        evidence_identity=evidence,
        bundle=bundle,
        tmp_path=tmp_path,
    )

    result = service.generate(run_id=run_id)

    assert result.comparison is None
    assert result.change is None
    assert len(repository.artifacts) == 1
    history = repository.load(result.history.analysis_artifact_id)
    assert history is not None
    assert history.artifact_type is AnalyticalArtifactType.HISTORY
    assert history.artifact_content["source_run_id"] == str(run_id)
    assert history.artifact_content["source_decision_run_id"] == str(decision_run_id)
    assert history.artifact_content["decision_artifact_hash"] == digest
    # No CHANGED/UNCHANGED classification and no comparison against guessed state.
    assert "classification" not in history.artifact_content
    assert "previous_run_id" not in history.artifact_content


# --- Case 2: valid previous run ----------------------------------------------


def test_valid_previous_run_loads_exact_id_and_generates_both_artefacts(
    tmp_path: Path,
) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_201)
    current_decision_run_id = UUID(int=85_202)
    previous_run_id = UUID(int=85_203)
    previous_decision_run_id = UUID(int=85_204)
    current_evidence = f"sha256:{'e' * 64}"
    previous_evidence = f"sha256:{'f' * 64}"
    current_bundle = _bundle(
        decision_run_id=current_decision_run_id, recommendation=_recommendation(offset=0)
    )
    previous_bundle = _bundle(
        decision_run_id=previous_decision_run_id, recommendation=_recommendation(offset=100)
    )
    current_reference, current_digest = _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=current_bundle,
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    previous_reference, previous_digest = _seed_run(
        ledger,
        run_id=previous_run_id,
        decision_run_id=previous_decision_run_id,
        evidence_identity=previous_evidence,
        bundle=previous_bundle,
        tmp_path=tmp_path,
    )
    current_provenance = DecisionProvenance(
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        decision_artifact_hash=current_digest,
    )
    previous_provenance = DecisionProvenance(
        run_id=previous_run_id,
        decision_run_id=previous_decision_run_id,
        evidence_identity=previous_evidence,
        decision_artifact_hash=previous_digest,
    )

    result = service.generate(run_id=current_run_id)

    assert result.history is not None
    assert result.comparison is not None
    assert result.change is RecommendationChange.CHANGED
    comparison = repository.load(result.comparison.analysis_artifact_id)
    assert comparison is not None
    assert comparison.artifact_type is AnalyticalArtifactType.COMPARISON
    assert comparison.source_decision_provenance == current_provenance
    assert comparison.compared_decisions == (previous_provenance,)
    content = comparison.artifact_content
    assert content["current_run_id"] == str(current_run_id)
    assert content["previous_run_id"] == str(previous_run_id)
    assert content["current_decision_artifact_hash"] == current_digest
    assert content["previous_decision_artifact_hash"] == previous_digest
    assert content["current_recommendation_identity"] == _identity_content(
        current_bundle.recommendation
    )
    assert content["previous_recommendation_identity"] == _identity_content(
        previous_bundle.recommendation
    )
    assert content["classification"] == "changed"
    history = repository.load(result.history.analysis_artifact_id)
    assert history is not None
    assert history.artifact_type is AnalyticalArtifactType.HISTORY


# --- Case 3: invalid/missing previous state ----------------------------------


def test_missing_previous_run_fails_explicitly_without_fallback(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_301)
    decision_run_id = UUID(int=85_302)
    missing_previous = UUID(int=85_399)
    evidence = f"sha256:{'e' * 64}"
    bundle = _bundle(decision_run_id=decision_run_id, recommendation=_recommendation())
    _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=decision_run_id,
        evidence_identity=evidence,
        bundle=bundle,
        tmp_path=tmp_path,
        previous_run_id=missing_previous,
    )
    before = ledger.get_raw(current_run_id)

    with pytest.raises(AnalyticalHistoryError, match="previous run"):
        service.generate(run_id=current_run_id)

    assert repository.artifacts == {}
    assert ledger.get_raw(current_run_id) == before
    record = records.get_run(current_run_id)
    assert isinstance(record, RunRecord)
    assert record.state is RunState.COMPLETED


def test_previous_run_without_recorded_decision_fails_explicitly(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_311)
    current_decision_run_id = UUID(int=85_312)
    previous_run_id = UUID(int=85_313)
    current_evidence = f"sha256:{'e' * 64}"
    previous_evidence = f"sha256:{'f' * 64}"
    current_bundle = _bundle(
        decision_run_id=current_decision_run_id, recommendation=_recommendation()
    )
    _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=current_bundle,
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    # Previous run exists but carries no recorded decision provenance.
    ledger.save(
        _completed_run(
            run_id=previous_run_id,
            decision_run_id=previous_run_id,
            evidence_identity=previous_evidence,
            decision_reference="state/missing.json",
            decision_hash="0" * 64,
        ).model_copy(update={"decisions": ()})
    )
    before = ledger.get_raw(current_run_id)

    with pytest.raises(AnalyticalHistoryError, match="no recorded decision"):
        service.generate(run_id=current_run_id)

    assert repository.artifacts == {}
    assert ledger.get_raw(current_run_id) == before


def test_previous_bundle_hash_drift_fails_explicitly(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_321)
    current_decision_run_id = UUID(int=85_322)
    previous_run_id = UUID(int=85_323)
    previous_decision_run_id = UUID(int=85_324)
    current_evidence = f"sha256:{'e' * 64}"
    previous_evidence = f"sha256:{'f' * 64}"
    current_bundle = _bundle(
        decision_run_id=current_decision_run_id, recommendation=_recommendation()
    )
    _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=current_bundle,
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    previous_reference, previous_digest = _seed_run(
        ledger,
        run_id=previous_run_id,
        decision_run_id=previous_decision_run_id,
        evidence_identity=previous_evidence,
        bundle=_bundle(
            decision_run_id=previous_decision_run_id, recommendation=_recommendation(offset=100)
        ),
        tmp_path=tmp_path,
    )
    # Tamper with the persisted previous bundle so its content hash drifts.
    Path(previous_reference).write_text('{"tampered": true}', encoding="utf-8")
    before = ledger.get_raw(current_run_id)

    with pytest.raises(AnalyticalHistoryError, match="cannot be loaded"):
        service.generate(run_id=current_run_id)

    assert repository.artifacts == {}
    assert ledger.get_raw(current_run_id) == before


def test_legacy_previous_run_fails_without_fabrication(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_331)
    current_decision_run_id = UUID(int=85_332)
    previous_run_id = UUID(int=85_333)
    current_evidence = f"sha256:{'e' * 64}"
    current_bundle = _bundle(
        decision_run_id=current_decision_run_id, recommendation=_recommendation()
    )
    _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=current_bundle,
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    # A sparse legacy document without schema_version is never fabricated into state.
    (ledger.root / f"{previous_run_id}.json").write_text(
        json.dumps(
            {"run_id": str(previous_run_id), "season": "2026-27", "gameweek": 1},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = ledger.get_raw(current_run_id)

    with pytest.raises(AnalyticalHistoryError, match="legacy"):
        service.generate(run_id=current_run_id)

    assert repository.artifacts == {}
    assert ledger.get_raw(current_run_id) == before


# --- Case 4: changed recommendation ------------------------------------------


def test_changed_recommendation_classifies_changed(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_401)
    current_decision_run_id = UUID(int=85_402)
    previous_run_id = UUID(int=85_403)
    previous_decision_run_id = UUID(int=85_404)
    current_evidence = f"sha256:{'e' * 64}"
    previous_evidence = f"sha256:{'f' * 64}"
    _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=_bundle(
            decision_run_id=current_decision_run_id, recommendation=_recommendation(offset=0)
        ),
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    _seed_run(
        ledger,
        run_id=previous_run_id,
        decision_run_id=previous_decision_run_id,
        evidence_identity=previous_evidence,
        bundle=_bundle(
            decision_run_id=previous_decision_run_id, recommendation=_recommendation(offset=50)
        ),
        tmp_path=tmp_path,
    )

    result = service.generate(run_id=current_run_id)

    assert result.change is RecommendationChange.CHANGED
    comparison = repository.load(result.comparison.analysis_artifact_id)
    assert comparison is not None
    assert comparison.artifact_content["classification"] == "changed"


# --- Case 5: unchanged recommendation with differing artefact hashes ----------


def test_unchanged_recommendation_with_differing_artefact_hashes(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_501)
    current_decision_run_id = UUID(int=85_502)
    previous_run_id = UUID(int=85_503)
    previous_decision_run_id = UUID(int=85_504)
    current_evidence = f"sha256:{'e' * 64}"
    previous_evidence = f"sha256:{'f' * 64}"
    shared_recommendation = _recommendation()
    current_bundle = _bundle(
        decision_run_id=current_decision_run_id,
        recommendation=shared_recommendation,
        config_fingerprint="config-current",
    )
    previous_bundle = _bundle(
        decision_run_id=previous_decision_run_id,
        recommendation=shared_recommendation,
        config_fingerprint="config-previous",
    )
    _, current_digest = _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=current_bundle,
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    _, previous_digest = _seed_run(
        ledger,
        run_id=previous_run_id,
        decision_run_id=previous_decision_run_id,
        evidence_identity=previous_evidence,
        bundle=previous_bundle,
        tmp_path=tmp_path,
    )
    assert current_digest != previous_digest

    result = service.generate(run_id=current_run_id)

    assert result.change is RecommendationChange.UNCHANGED
    comparison = repository.load(result.comparison.analysis_artifact_id)
    assert comparison is not None
    content = comparison.artifact_content
    assert content["classification"] == "unchanged"
    assert content["current_decision_artifact_hash"] == current_digest
    assert content["previous_decision_artifact_hash"] == previous_digest
    assert content["current_recommendation_identity"] == content[
        "previous_recommendation_identity"
    ]


# --- DecisionBundleV1 read/parse seam ----------------------------------------


def test_parse_decision_bundle_round_trips_canonical_bytes() -> None:
    bundle = _bundle(decision_run_id=UUID(int=85_601), recommendation=_recommendation())

    parsed = parse_decision_bundle(serialize_decision_bundle(bundle))

    assert parsed == bundle
    assert parsed.recommendation.identity == bundle.recommendation.identity
    assert parsed.recommendation.formation == bundle.recommendation.formation


def test_load_decision_bundle_verifies_recorded_content_hash(tmp_path: Path) -> None:
    bundle = _bundle(decision_run_id=UUID(int=85_611), recommendation=_recommendation())
    reference, digest = _persist_bundle(bundle, tmp_path)

    assert load_decision_bundle(reference=reference, sha256=digest) == bundle

    Path(reference).write_text('{"tampered": true}', encoding="utf-8")
    with pytest.raises(DecisionBundleError, match="hash mismatch"):
        load_decision_bundle(reference=reference, sha256=digest)


def test_load_decision_bundle_rejects_missing_reference(tmp_path: Path) -> None:
    with pytest.raises(DecisionBundleError, match="cannot read"):
        load_decision_bundle(reference=str(tmp_path / "missing.json"), sha256="0" * 64)


def test_parse_decision_bundle_rejects_invalid_content() -> None:
    with pytest.raises(DecisionBundleError, match="not valid JSON"):
        parse_decision_bundle(b"not-json")
    with pytest.raises(DecisionBundleError, match="invalid decision bundle"):
        parse_decision_bundle(b'{"schema_version": 1}')


# --- Execution boundary ------------------------------------------------------


def test_generate_requires_completed_run(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    run_id = UUID(int=85_701)
    decision_run_id = UUID(int=85_702)
    evidence = f"sha256:{'e' * 64}"
    bundle = _bundle(decision_run_id=decision_run_id, recommendation=_recommendation())
    reference, digest = _persist_bundle(bundle, tmp_path)
    ledger.save(
        _completed_run(
            run_id=run_id,
            decision_run_id=decision_run_id,
            evidence_identity=evidence,
            decision_reference=reference,
            decision_hash=digest,
            state=RunState.PROVISIONAL,
        )
    )

    with pytest.raises(AnalyticalHistoryError, match="completed baseline run"):
        service.generate(run_id=run_id)

    assert repository.artifacts == {}


def test_analytical_failure_never_mutates_completed_run(tmp_path: Path) -> None:
    service, records, repository, ledger = _setup(tmp_path)
    current_run_id = UUID(int=85_711)
    current_decision_run_id = UUID(int=85_712)
    previous_run_id = UUID(int=85_713)
    previous_decision_run_id = UUID(int=85_714)
    current_evidence = f"sha256:{'e' * 64}"
    previous_evidence = f"sha256:{'f' * 64}"
    _seed_run(
        ledger,
        run_id=current_run_id,
        decision_run_id=current_decision_run_id,
        evidence_identity=current_evidence,
        bundle=_bundle(
            decision_run_id=current_decision_run_id, recommendation=_recommendation()
        ),
        tmp_path=tmp_path,
        previous_run_id=previous_run_id,
    )
    previous_reference, _ = _seed_run(
        ledger,
        run_id=previous_run_id,
        decision_run_id=previous_decision_run_id,
        evidence_identity=previous_evidence,
        bundle=_bundle(
            decision_run_id=previous_decision_run_id, recommendation=_recommendation(offset=100)
        ),
        tmp_path=tmp_path,
    )
    Path(previous_reference).unlink()
    before = ledger.get_raw(current_run_id)

    with pytest.raises(AnalyticalHistoryError, match="cannot be loaded"):
        service.generate(run_id=current_run_id)

    assert ledger.get_raw(current_run_id) == before
    record = records.get_run(current_run_id)
    assert isinstance(record, RunRecord)
    assert record.state is RunState.COMPLETED
    assert repository.artifacts == {}
