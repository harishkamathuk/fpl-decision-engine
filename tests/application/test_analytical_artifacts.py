"""Application tests for downstream immutable analytical artefact generation."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

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
    calculate_analysis_artifact_id,
)
from fpl_decision_engine.ports import (
    AnalyticalArtifactError,
    AnalyticalArtifactGenerator,
    ComparisonGeneratorInput,
    GeneratorInputData,
    HistoryGeneratorInput,
    PersistedAnalyticalArtifact,
    ReviewGeneratorInput,
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
    def __init__(self) -> None:
        self.received: list[
            HistoryGeneratorInput | ComparisonGeneratorInput | ReviewGeneratorInput
        ] = []

    def generate(
        self,
        *,
        generator_input: (
            HistoryGeneratorInput | ComparisonGeneratorInput | ReviewGeneratorInput
        ),
    ) -> dict[str, object]:
        self.received.append(generator_input)
        if isinstance(generator_input, HistoryGeneratorInput):
            return generator_input.history_inputs.as_content()
        if isinstance(generator_input, ComparisonGeneratorInput):
            return generator_input.comparison_inputs.as_content()
        return generator_input.review_inputs.as_content()


def history_input(
    *,
    source_run_id: UUID = RUN_ID,
    source_decision_run_id: UUID | None = DECISION_RUN_ID,
    evidence_identity: str = EVIDENCE_IDENTITY,
    generator_name: str = "synthetic-analysis",
    generator_version: str = "1.0.0",
    history_inputs: dict[str, object] | None = None,
) -> HistoryGeneratorInput:
    return HistoryGeneratorInput(
        source_run_id=source_run_id,
        source_decision_run_id=source_decision_run_id,
        evidence_identity=evidence_identity,
        generator_name=generator_name,
        generator_version=generator_version,
        history_inputs=GeneratorInputData.from_content(
            history_inputs
            if history_inputs is not None
            else {"points": [1, 2, 3], "summary": "stable"}
        ),
    )


def comparison_input() -> ComparisonGeneratorInput:
    return ComparisonGeneratorInput(
        source_run_id=RUN_ID,
        source_decision_run_id=DECISION_RUN_ID,
        evidence_identity=EVIDENCE_IDENTITY,
        generator_name="synthetic-analysis",
        generator_version="1.0.0",
        comparison_inputs=GeneratorInputData.from_content(
            {"baseline": 72, "candidate": 75}
        ),
    )


def review_input() -> ReviewGeneratorInput:
    return ReviewGeneratorInput(
        source_run_id=RUN_ID,
        source_decision_run_id=DECISION_RUN_ID,
        evidence_identity=EVIDENCE_IDENTITY,
        generator_name="synthetic-analysis",
        generator_version="1.0.0",
        review_inputs=GeneratorInputData.from_content(
            {"decision": "captain-a", "outcome_points": 12}
        ),
    )


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
        generator_input=history_input(),
        generator=Generator(),
        created_at=NOW,
    )
    second = service.generate_history(
        source_run=source_run,
        generator_input=history_input(
            history_inputs={"summary": "stable", "points": [1, 2, 3]}
        ),
        generator=Generator(),
        created_at=NOW + timedelta(hours=1),
    )

    assert first.analysis_artifact_id == second.analysis_artifact_id
    assert len(repository.artifacts) == 1


def test_generator_receives_complete_explicit_immutable_input() -> None:
    repository = MemoryRepository()
    generator = Generator()
    generator_input = history_input(
        history_inputs={
            "resolved_runs": ["run-a", "run-b"],
            "decision_points": [72, 75],
        }
    )

    result = AnalyticalArtifactService(repository).generate_history(
        source_run=completed_run(),
        generator_input=generator_input,
        generator=generator,
        created_at=NOW,
    )

    assert generator.received == [generator_input]
    assert generator_input.source_run_id == RUN_ID
    assert generator_input.source_decision_run_id == DECISION_RUN_ID
    assert generator_input.evidence_identity == EVIDENCE_IDENTITY
    assert generator_input.generator_name == "synthetic-analysis"
    assert generator_input.generator_version == "1.0.0"
    assert generator_input.history_inputs.as_content() == {
        "resolved_runs": ["run-a", "run-b"],
        "decision_points": [72, 75],
    }
    with pytest.raises(ValidationError, match="frozen"):
        generator_input.generator_version = "2.0.0"
    with pytest.raises(ValidationError, match="frozen"):
        generator_input.history_inputs.canonical_json = "{}"
    mutable_copy = generator_input.history_inputs.as_content()
    mutable_copy["new-value"] = True
    assert "new-value" not in generator_input.history_inputs.as_content()


    artifact = repository.load(result.analysis_artifact_id)
    assert artifact is not None
    assert result.analysis_artifact_id == calculate_analysis_artifact_id(
        schema_version=artifact.schema_version,
        artifact_type=AnalyticalArtifactType.HISTORY,
        source_run_id=generator_input.source_run_id,
        source_decision_run_id=generator_input.source_decision_run_id,
        evidence_identity=generator_input.evidence_identity,
        generator_name=generator_input.generator_name,
        generator_version=generator_input.generator_version,
        artifact_content=generator_input.history_inputs.as_content(),
    )


def test_generator_contract_has_no_repository_or_run_record_dependency() -> None:
    signature = inspect.signature(AnalyticalArtifactGenerator.generate)

    assert tuple(signature.parameters) == ("self", "generator_input")
    expected_common = {
        "source_run_id",
        "source_decision_run_id",
        "evidence_identity",
        "generator_name",
        "generator_version",
    }
    assert set(HistoryGeneratorInput.model_fields) == expected_common | {"history_inputs"}
    assert set(ComparisonGeneratorInput.model_fields) == expected_common | {
        "comparison_inputs"
    }
    assert set(ReviewGeneratorInput.model_fields) == expected_common | {"review_inputs"}
    for contract in (
        HistoryGeneratorInput,
        ComparisonGeneratorInput,
        ReviewGeneratorInput,
    ):
        assert "repository" not in contract.model_fields
        assert "run_record" not in contract.model_fields


@pytest.mark.parametrize("changed", ["generator", "version", "content"])
def test_regeneration_changes_identity_for_semantic_change(changed: str) -> None:
    repository = MemoryRepository()
    service = AnalyticalArtifactService(repository)
    source_run = completed_run()
    original_input = history_input()
    if changed == "generator":
        changed_input = history_input(generator_name="different-analysis")
    elif changed == "version":
        changed_input = history_input(generator_version="2.0.0")
    else:
        changed_input = history_input(
            history_inputs={"points": [4], "summary": "changed"}
        )

    original = service.generate_history(
        source_run=source_run,
        generator_input=original_input,
        generator=Generator(),
        created_at=NOW,
    )
    regenerated = service.generate_history(
        source_run=source_run,
        generator_input=changed_input,
        generator=Generator(),
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
            generator_input=history_input(),
            generator=generator,
            created_at=NOW,
        ),
        service.generate_comparison(
            source_run=source_run,
            generator_input=comparison_input(),
            generator=generator,
            created_at=NOW,
        ),
        service.generate_review(
            source_run=source_run,
            generator_input=review_input(),
            generator=generator,
            created_at=NOW,
        ),
    )

    assert generator.received == [
        history_input(),
        comparison_input(),
        review_input(),
    ]
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
        service.generate_history(
            source_run=source_run,
            generator_input=history_input(),
            generator=Generator(),
            created_at=NOW,
        )


@pytest.mark.parametrize(
    ("generator_input", "message"),
    [
        (
            history_input(source_run_id=UUID(int=104_001)),
            "source_run_id does not match",
        ),
        (
            history_input(evidence_identity=f"sha256:{'f' * 64}"),
            "evidence_identity does not match",
        ),
    ],
)
def test_generator_input_provenance_must_match_loaded_run(
    generator_input: HistoryGeneratorInput,
    message: str,
) -> None:
    generator = Generator()

    with pytest.raises(AnalyticalArtifactError, match=message):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=completed_run(),
            generator_input=generator_input,
            generator=generator,
            created_at=NOW,
        )

    assert generator.received == []


def test_invalid_source_is_rejected_before_generator_runs() -> None:
    class MustNotRun(Generator):
        called = False

        def generate(
            self,
            *,
            generator_input: HistoryGeneratorInput,
        ) -> dict[str, object]:
            self.called = True
            raise AssertionError("generator must not run")

    generator = MustNotRun()

    with pytest.raises(AnalyticalArtifactError, match="require a completed run"):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=completed_run(state=RunState.PROVISIONAL),
            generator_input=history_input(),
            generator=generator,
            created_at=NOW,
        )

    assert generator.called is False


def test_generation_never_mutates_run_record_even_when_generator_fails() -> None:
    class FailingGenerator(Generator):
        def generate(
            self,
            *,
            generator_input: HistoryGeneratorInput,
        ) -> dict[str, object]:
            raise RuntimeError("analysis unavailable")

    source_run = completed_run()
    original = source_run.model_dump_json()

    with pytest.raises(RuntimeError, match="analysis unavailable"):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=source_run,
            generator_input=history_input(),
            generator=FailingGenerator(),
            created_at=NOW,
        )

    assert source_run.model_dump_json() == original
    assert source_run.state is RunState.COMPLETED
