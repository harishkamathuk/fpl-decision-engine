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
    AnalyticalArtifactRef,
    AnalyticalArtifactType,
    AuthorityEvent,
    DecisionProvenance,
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
SOURCE_PROVENANCE = DecisionProvenance(
    run_id=RUN_ID,
    decision_run_id=DECISION_RUN_ID,
    evidence_identity=EVIDENCE_IDENTITY,
    decision_artifact_hash="c" * 64,
)
COMPARED_PROVENANCE = DecisionProvenance(
    run_id=UUID(int=85_003),
    decision_run_id=UUID(int=85_004),
    evidence_identity=f"sha256:{'d' * 64}",
    decision_artifact_hash="f" * 64,
)
REFERENCED_ARTIFACT = AnalyticalArtifactRef(
    artifact_id=f"sha256:{'1' * 64}",
    artifact_type=AnalyticalArtifactType.HISTORY,
    content_hash="2" * 64,
    source_run_id=RUN_ID,
    source_decision_run_id=DECISION_RUN_ID,
    evidence_identity=EVIDENCE_IDENTITY,
    schema_version=2,
    generator_name="history",
    generator_version="1.0.0",
)


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
    source_decision: DecisionProvenance = SOURCE_PROVENANCE,
    generator_name: str = "synthetic-analysis",
    generator_version: str = "1.0.0",
    history_inputs: dict[str, object] | None = None,
) -> HistoryGeneratorInput:
    return HistoryGeneratorInput(
        source_decision=source_decision,
        generator_name=generator_name,
        generator_version=generator_version,
        history_inputs=GeneratorInputData.from_content(
            history_inputs
            if history_inputs is not None
            else {"points": [1, 2, 3], "summary": "stable"}
        ),
    )


def comparison_input(
    *,
    source_decision: DecisionProvenance = SOURCE_PROVENANCE,
    compared_decisions: tuple[DecisionProvenance, ...] = (COMPARED_PROVENANCE,),
) -> ComparisonGeneratorInput:
    return ComparisonGeneratorInput(
        source_decision=source_decision,
        compared_decisions=compared_decisions,
        generator_name="synthetic-analysis",
        generator_version="1.0.0",
        comparison_inputs=GeneratorInputData.from_content(
            {"baseline": 72, "candidate": 75}
        ),
    )


def review_input(
    *,
    source_decision: DecisionProvenance = SOURCE_PROVENANCE,
    referenced_artifacts: tuple[AnalyticalArtifactRef, ...] = (REFERENCED_ARTIFACT,),
) -> ReviewGeneratorInput:
    return ReviewGeneratorInput(
        source_decision=source_decision,
        referenced_artifacts=referenced_artifacts,
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
    source_provenance: DecisionProvenance = SOURCE_PROVENANCE,
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
            provenance=source_provenance,
            recorded_at=NOW,
        ),
    ) if with_decision and evidence_identity is not None else ()
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
        run_id=source_provenance.run_id,
        season="2026-27",
        gameweek=1,
        created_at=NOW - timedelta(minutes=2),
        mandatory_stages=("baseline",),
        state=state,
        stage_attempts=stage_attempts if state is not RunState.PROVISIONAL else (),
        artefacts=evidence_artefacts,
        decisions=decisions,
        authority_events=(
            (AuthorityEvent(approved_at=NOW, by="operator", reason="approved"),)
            if state is RunState.AUTHORITATIVE
            else ()
        ),
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
    assert generator_input.source_decision == SOURCE_PROVENANCE
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
        source_run_id=generator_input.source_decision.run_id,
        source_decision_run_id=generator_input.source_decision.decision_run_id,
        evidence_identity=generator_input.source_decision.evidence_identity,
        generator_name=generator_input.generator_name,
        generator_version=generator_input.generator_version,
        artifact_content=generator_input.history_inputs.as_content(),
        source_decision_provenance=generator_input.source_decision,
    )


def test_generator_contract_has_no_repository_or_run_record_dependency() -> None:
    signature = inspect.signature(AnalyticalArtifactGenerator.generate)

    assert tuple(signature.parameters) == ("self", "generator_input")
    expected_common = {
        "source_decision",
        "generator_name",
        "generator_version",
    }
    assert set(HistoryGeneratorInput.model_fields) == expected_common | {"history_inputs"}
    assert set(ComparisonGeneratorInput.model_fields) == expected_common | {
        "compared_decisions",
        "comparison_inputs",
    }
    assert set(ReviewGeneratorInput.model_fields) == expected_common | {
        "referenced_artifacts",
        "review_inputs",
    }
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


@pytest.mark.parametrize("state", [RunState.COMPLETED, RunState.AUTHORITATIVE])
def test_history_comparison_and_review_preserve_provenance(state: RunState) -> None:
    repository = MemoryRepository()
    service = AnalyticalArtifactService(repository)
    source_run = completed_run(state=state)
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
            history_input(
                source_decision=SOURCE_PROVENANCE.model_copy(
                    update={"run_id": UUID(int=104_001)}
                )
            ),
            "source_run_id does not match",
        ),
        (
            history_input(
                source_decision=SOURCE_PROVENANCE.model_copy(
                    update={"evidence_identity": f"sha256:{'f' * 64}"}
                )
            ),
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


@pytest.mark.parametrize("state", [RunState.COMPLETED, RunState.AUTHORITATIVE])
def test_generation_never_mutates_run_record_even_when_generator_fails(
    state: RunState,
) -> None:
    class FailingGenerator(Generator):
        def generate(
            self,
            *,
            generator_input: HistoryGeneratorInput,
        ) -> dict[str, object]:
            raise RuntimeError("analysis unavailable")

    source_run = completed_run(state=state)
    original = source_run.model_dump_json()

    with pytest.raises(RuntimeError, match="analysis unavailable"):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=source_run,
            generator_input=history_input(),
            generator=FailingGenerator(),
            created_at=NOW,
        )

    assert source_run.model_dump_json() == original
    assert source_run.state is state


@pytest.mark.parametrize(
    ("contract", "payload", "message"),
    [
        (
            HistoryGeneratorInput,
            {
                "generator_name": "analysis",
                "generator_version": "1",
                "history_inputs": GeneratorInputData.from_content({"value": 1}),
            },
            "source_decision",
        ),
        (
            ComparisonGeneratorInput,
            {
                "source_decision": SOURCE_PROVENANCE,
                "compared_decisions": (),
                "generator_name": "analysis",
                "generator_version": "1",
                "comparison_inputs": GeneratorInputData.from_content({"value": 1}),
            },
            "at least 1 item",
        ),
        (
            ReviewGeneratorInput,
            {
                "source_decision": SOURCE_PROVENANCE,
                "referenced_artifacts": (),
                "generator_name": "analysis",
                "generator_version": "1",
                "review_inputs": GeneratorInputData.from_content({"value": 1}),
            },
            "at least 1 item",
        ),
        (
            ReviewGeneratorInput,
            {
                "referenced_artifacts": (REFERENCED_ARTIFACT,),
                "generator_name": "analysis",
                "generator_version": "1",
                "review_inputs": GeneratorInputData.from_content({"value": 1}),
            },
            "source_decision",
        ),
    ],
)
def test_required_typed_provenance_fails_before_generation(
    contract: type[
        HistoryGeneratorInput | ComparisonGeneratorInput | ReviewGeneratorInput
    ],
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        contract.model_validate(payload)


def test_decision_provenance_requires_evidence_identity() -> None:
    with pytest.raises(ValidationError, match="evidence_identity"):
        DecisionProvenance.model_validate(
            {
                "run_id": RUN_ID,
                "decision_run_id": DECISION_RUN_ID,
                "decision_artifact_hash": "c" * 64,
            }
        )


def test_comparison_rejects_conflicting_decision_provenance() -> None:
    conflicting = COMPARED_PROVENANCE.model_copy(
        update={"decision_artifact_hash": "e" * 64}
    )

    with pytest.raises(ValidationError, match="conflicting duplicate provenance"):
        comparison_input(compared_decisions=(conflicting, COMPARED_PROVENANCE))


def test_source_decision_provenance_must_match_recorded_decision() -> None:
    changed = SOURCE_PROVENANCE.model_copy(
        update={"decision_artifact_hash": "d" * 64}
    )
    generator = Generator()

    with pytest.raises(AnalyticalArtifactError, match="not recorded by the RunRecord"):
        AnalyticalArtifactService(MemoryRepository()).generate_history(
            source_run=completed_run(),
            generator_input=history_input(source_decision=changed),
            generator=generator,
            created_at=NOW,
        )

    assert generator.received == []


@pytest.mark.parametrize("changed", ["source_run", "decision_run", "decision_hash"])
def test_source_decision_provenance_changes_v2_identity(changed: str) -> None:
    if changed == "source_run":
        update: dict[str, object] = {"run_id": UUID(int=105_001)}
    elif changed == "decision_run":
        update = {"decision_run_id": UUID(int=105_002)}
    else:
        update = {"decision_artifact_hash": "d" * 64}
    changed_provenance = SOURCE_PROVENANCE.model_copy(update=update)
    service = AnalyticalArtifactService(MemoryRepository())

    original = service.generate_history(
        source_run=completed_run(),
        generator_input=history_input(),
        generator=Generator(),
        created_at=NOW,
    )
    changed_artifact = service.generate_history(
        source_run=completed_run(source_provenance=changed_provenance),
        generator_input=history_input(source_decision=changed_provenance),
        generator=Generator(),
        created_at=NOW,
    )

    assert changed_artifact.analysis_artifact_id != original.analysis_artifact_id


def test_compared_decision_changes_identity_even_when_content_is_identical() -> None:
    candidate_c = DecisionProvenance(
        run_id=UUID(int=105_002),
        decision_run_id=UUID(int=105_003),
        evidence_identity=f"sha256:{'a' * 64}",
        decision_artifact_hash="b" * 64,
    )
    service = AnalyticalArtifactService(MemoryRepository())

    versus_b = service.generate_comparison(
        source_run=completed_run(),
        generator_input=comparison_input(),
        generator=Generator(),
        created_at=NOW,
    )
    versus_c = service.generate_comparison(
        source_run=completed_run(),
        generator_input=comparison_input(compared_decisions=(candidate_c,)),
        generator=Generator(),
        created_at=NOW,
    )

    assert versus_c.analysis_artifact_id != versus_b.analysis_artifact_id


def test_referenced_artefact_changes_identity_even_when_content_is_identical() -> None:
    reference_y = REFERENCED_ARTIFACT.model_copy(
        update={"artifact_id": f"sha256:{'3' * 64}", "content_hash": "4" * 64}
    )
    service = AnalyticalArtifactService(MemoryRepository())

    review_x = service.generate_review(
        source_run=completed_run(),
        generator_input=review_input(),
        generator=Generator(),
        created_at=NOW,
    )
    review_y = service.generate_review(
        source_run=completed_run(),
        generator_input=review_input(referenced_artifacts=(reference_y,)),
        generator=Generator(),
        created_at=NOW,
    )

    assert review_y.analysis_artifact_id != review_x.analysis_artifact_id
