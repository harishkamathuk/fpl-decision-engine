"""Application tests for downstream immutable analytical artefact generation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fpl_decision_engine.application import AnalyticalArtifactService
from fpl_decision_engine.domain import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    AnalyticalArtifact,
    AnalyticalArtifactType,
    RecordedDecision,
    RunArtefact,
    RunRecord,
    RunState,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.ports import (
    AnalyticalArtifactError,
    PersistedAnalyticalArtifact,
)

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
RUN_ID = UUID(int=85_001)
DECISION_RUN_ID = UUID(int=85_002)
EVIDENCE_IDENTITY = f"sha256:{'e' * 64}"


class MemoryRepository:
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


class Generator:
    generator_name = "synthetic-analysis"
    generator_version = "1.0.0"

    def __init__(self, content: dict[str, object] | None = None) -> None:
        self.content = content or {"points": [1, 2, 3], "summary": "stable"}

    def generate_history(self, *, source_run: RunRecord) -> dict[str, object]:
        return self.content

    def generate_comparison(self, *, source_run: RunRecord) -> dict[str, object]:
        return self.content

    def generate_review(self, *, source_run: RunRecord) -> dict[str, object]:
        return self.content


def completed_run(
    *,
    evidence_identity: str | None = EVIDENCE_IDENTITY,
    with_decision: bool = True,
    state: RunState = RunState.COMPLETED,
) -> RunRecord:
    evidence_artefacts = (
        RunArtefact(
            name="gameweek-evidence",
            reference="state/evidence.json",
            sha256="b" * 64,
            kind=GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
            recorded_at=NOW,
        ),
    ) if evidence_identity is not None else ()
    decisions = (
        RecordedDecision(
            reference="state/decision.json",
            sha256="c" * 64,
            recorded_at=NOW,
        ),
    ) if with_decision else ()
    stage_attempts = (
        StageAttempt(
            stage="baseline",
            attempt=1,
            status=StageState.PASS,
            started_at=NOW - timedelta(minutes=1),
            finished_at=NOW,
        ),
    )
    return RunRecord(
        run_id=RUN_ID,
        season="2026-27",
        gameweek=1,
        created_at=NOW - timedelta(minutes=2),
        mandatory_stages=("baseline",),
        state=state,
        stage_attempts=stage_attempts if state is not RunState.PROVISIONAL else (),
        artefacts=evidence_artefacts,
        decisions=decisions,
        closed_at=NOW if state is not RunState.PROVISIONAL else None,
        evidence_identity=evidence_identity,
    )


def test_identical_semantics_have_deterministic_identity() -> None:
    repository = MemoryRepository()
    service = AnalyticalArtifactService(repository)
    source_run = completed_run()

    first = service.generate_history(
        source_run=source_run,
        source_decision_run_id=DECISION_RUN_ID,
        generator=Generator(),
        created_at=NOW,
    )
    second = service.generate_history(
        source_run=source_run,
        source_decision_run_id=DECISION_RUN_ID,
        generator=Generator({"summary": "stable", "points": [1, 2, 3]}),
        created_at=NOW + timedelta(hours=1),
    )

    assert first.analysis_artifact_id == second.analysis_artifact_id
    assert len(repository.artifacts) == 1


@pytest.mark.parametrize("changed", ["generator", "version", "content"])
def test_regeneration_changes_identity_for_semantic_change(changed: str) -> None:
    repository = MemoryRepository()
    service = AnalyticalArtifactService(repository)
    source_run = completed_run()
    original_generator = Generator()
    changed_generator = Generator({"points": [4], "summary": "changed"})
    if changed == "generator":
        changed_generator.generator_name = "different-analysis"
        changed_generator.content = original_generator.content
    elif changed == "version":
        changed_generator.generator_version = "2.0.0"
        changed_generator.content = original_generator.content

    original = service.generate_history(
        source_run=source_run,
        generator=original_generator,
        created_at=NOW,
    )
    regenerated = service.generate_history(
        source_run=source_run,
        generator=changed_generator,
        created_at=NOW,
    )

    assert regenerated.analysis_artifact_id != original.analysis_artifact_id


def test_history_comparison_and_review_preserve_provenance() -> None:
    repository = MemoryRepository()
    service = AnalyticalArtifactService(repository)
    source_run = completed_run()
    generator = Generator()
    original_run = source_run.model_dump_json()

    generated = (
        service.generate_history(
            source_run=source_run,
            source_decision_run_id=DECISION_RUN_ID,
            generator=generator,
            created_at=NOW,
        ),
        service.generate_comparison(
            source_run=source_run,
            source_decision_run_id=DECISION_RUN_ID,
            generator=generator,
            created_at=NOW,
        ),
        service.generate_review(
            source_run=source_run,
            source_decision_run_id=DECISION_RUN_ID,
            generator=generator,
            created_at=NOW,
        ),
    )

    artifacts = tuple(repository.load(item.analysis_artifact_id) for item in generated)
    assert all(artifact is not None for artifact in artifacts)
    assert tuple(artifact.artifact_type for artifact in artifacts if artifact is not None) == (
        AnalyticalArtifactType.HISTORY,
        AnalyticalArtifactType.COMPARISON,
        AnalyticalArtifactType.REVIEW,
    )
    for artifact in artifacts:
        assert artifact is not None
        assert artifact.source_run_id == source_run.run_id
        assert artifact.source_decision_run_id == DECISION_RUN_ID
        assert artifact.evidence_identity == source_run.evidence_identity
    assert source_run.model_dump_json() == original_run


@pytest.mark.parametrize(
    ("source_run", "message"),
    [
        (completed_run(state=RunState.PROVISIONAL), "require a completed run"),
        (completed_run(evidence_identity=None), "evidence-bound run"),
        (completed_run(with_decision=False), "decision provenance"),
    ],
)
def test_generation_requires_completed_evidence_bound_decision_run(
    source_run: RunRecord, message: str
) -> None:
    service = AnalyticalArtifactService(MemoryRepository())

    with pytest.raises(AnalyticalArtifactError, match=message):
        service.generate_history(source_run=source_run, generator=Generator(), created_at=NOW)


def test_invalid_source_is_rejected_before_generator_runs() -> None:
    class MustNotRun(Generator):
        called = False

        def generate_history(self, *, source_run: RunRecord) -> dict[str, object]:
            self.called = True
            raise AssertionError("generator must not run")

    generator = MustNotRun()

    with pytest.raises(AnalyticalArtifactError, match="require a completed run"):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=completed_run(state=RunState.PROVISIONAL),
            generator=generator,
            created_at=NOW,
        )

    assert generator.called is False


def test_generation_never_mutates_run_record_even_when_generator_fails() -> None:
    class FailingGenerator(Generator):
        def generate_history(self, *, source_run: RunRecord) -> dict[str, object]:
            raise RuntimeError("analysis unavailable")

    source_run = completed_run()
    original = source_run.model_dump_json()

    with pytest.raises(RuntimeError, match="analysis unavailable"):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=source_run,
            generator=FailingGenerator(),
            created_at=NOW,
        )

    assert source_run.model_dump_json() == original
    assert source_run.state is RunState.COMPLETED
