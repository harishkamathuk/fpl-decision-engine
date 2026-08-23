"""Generate immutable analytical artefacts downstream of completed decision runs."""

from __future__ import annotations

from datetime import datetime

from fpl_decision_engine.domain.analytical_artifact import (
    ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
    AnalyticalArtifact,
    AnalyticalArtifactRef,
    AnalyticalArtifactType,
    calculate_analysis_artifact_id,
    calculate_analytical_content_hash,
)
from fpl_decision_engine.domain.provenance import DecisionProvenance
from fpl_decision_engine.domain.run_record import RecordedDecision, RunRecord, RunState
from fpl_decision_engine.ports.analytical_artifacts import (
    AnalyticalArtifactError,
    AnalyticalArtifactGenerator,
    AnalyticalArtifactRepository,
    AnalyticalContent,
    ComparisonGeneratorInput,
    HistoryGeneratorInput,
    PersistedAnalyticalArtifact,
    ReviewGeneratorInput,
)

GeneratorInput = HistoryGeneratorInput | ComparisonGeneratorInput | ReviewGeneratorInput


class AnalyticalArtifactService:
    """Validate explicit generator inputs and publish their derived outputs.

    The calling layer supplies already-loaded, resolved values. Generators never receive
    a RunRecord or repository and this service never mutates run lifecycle.
    """

    def __init__(self, repository: AnalyticalArtifactRepository) -> None:
        self._repository = repository

    def generate_history(
        self,
        *,
        source_run: RunRecord,
        generator_input: HistoryGeneratorInput,
        generator: AnalyticalArtifactGenerator[HistoryGeneratorInput],
        created_at: datetime,
    ) -> PersistedAnalyticalArtifact:
        """Generate and publish history content for a completed run."""

        self._validate_generator_input(source_run, generator_input)
        return self._publish(
            source_run=source_run,
            source_decision=generator_input.source_decision,
            artifact_type=AnalyticalArtifactType.HISTORY,
            generator_name=generator_input.generator_name,
            generator_version=generator_input.generator_version,
            created_at=created_at,
            artifact_content=generator.generate(generator_input=generator_input),
        )

    def generate_comparison(
        self,
        *,
        source_run: RunRecord,
        generator_input: ComparisonGeneratorInput,
        generator: AnalyticalArtifactGenerator[ComparisonGeneratorInput],
        created_at: datetime,
    ) -> PersistedAnalyticalArtifact:
        """Generate and publish comparison content for a completed run."""

        self._validate_generator_input(source_run, generator_input)
        return self._publish(
            source_run=source_run,
            source_decision=generator_input.source_decision,
            artifact_type=AnalyticalArtifactType.COMPARISON,
            generator_name=generator_input.generator_name,
            generator_version=generator_input.generator_version,
            created_at=created_at,
            artifact_content=generator.generate(generator_input=generator_input),
            compared_decisions=generator_input.compared_decisions,
        )

    def generate_review(
        self,
        *,
        source_run: RunRecord,
        generator_input: ReviewGeneratorInput,
        generator: AnalyticalArtifactGenerator[ReviewGeneratorInput],
        created_at: datetime,
    ) -> PersistedAnalyticalArtifact:
        """Generate and publish review content for a completed run."""

        self._validate_generator_input(source_run, generator_input)
        return self._publish(
            source_run=source_run,
            source_decision=generator_input.source_decision,
            artifact_type=AnalyticalArtifactType.REVIEW,
            generator_name=generator_input.generator_name,
            generator_version=generator_input.generator_version,
            created_at=created_at,
            artifact_content=generator.generate(generator_input=generator_input),
            referenced_artifacts=generator_input.referenced_artifacts,
        )

    @staticmethod
    def _validate_source_run(source_run: RunRecord) -> str:
        if source_run.state not in (RunState.COMPLETED, RunState.AUTHORITATIVE):
            raise AnalyticalArtifactError(
                f"analytical artefacts require a completed run; {source_run.run_id} is "
                f"{source_run.state.value}"
            )
        evidence_identity = source_run.evidence_identity
        if evidence_identity is None:
            raise AnalyticalArtifactError(
                f"analytical artefacts require an evidence-bound run; {source_run.run_id} "
                "has no evidence_identity"
            )
        if not source_run.decisions:
            raise AnalyticalArtifactError(
                f"analytical artefacts require recorded decision provenance; "
                f"{source_run.run_id} has no decision artefact"
            )
        return evidence_identity

    def _validate_generator_input(
        self,
        source_run: RunRecord,
        generator_input: GeneratorInput,
    ) -> None:
        evidence_identity = self._validate_source_run(source_run)
        provenance = generator_input.source_decision
        if provenance.run_id != source_run.run_id:
            raise AnalyticalArtifactError(
                "generator input source_run_id does not match the validated RunRecord: "
                f"{provenance.run_id} != {source_run.run_id}"
            )
        if provenance.evidence_identity != evidence_identity:
            raise AnalyticalArtifactError(
                "generator input evidence_identity does not match the validated RunRecord: "
                f"{provenance.evidence_identity} != {evidence_identity}"
            )
        if not any(
            isinstance(decision, RecordedDecision) and decision.provenance == provenance
            for decision in source_run.decisions
        ):
            raise AnalyticalArtifactError(
                "generator input source decision provenance is not recorded by the RunRecord"
            )

    def _publish(
        self,
        *,
        source_run: RunRecord,
        source_decision: DecisionProvenance,
        artifact_type: AnalyticalArtifactType,
        generator_name: str,
        generator_version: str,
        created_at: datetime,
        artifact_content: AnalyticalContent,
        compared_decisions: tuple[DecisionProvenance, ...] = (),
        referenced_artifacts: tuple[AnalyticalArtifactRef, ...] = (),
    ) -> PersistedAnalyticalArtifact:
        content_hash = calculate_analytical_content_hash(artifact_content)
        artifact_id = calculate_analysis_artifact_id(
            schema_version=ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
            artifact_type=artifact_type,
            source_run_id=source_run.run_id,
            source_decision_run_id=source_decision.decision_run_id,
            evidence_identity=source_decision.evidence_identity,
            generator_name=generator_name,
            generator_version=generator_version,
            artifact_content=artifact_content,
            source_decision_provenance=source_decision,
            compared_decisions=compared_decisions,
            referenced_artifacts=referenced_artifacts,
        )
        artifact = AnalyticalArtifact(
            analysis_artifact_id=artifact_id,
            source_run_id=source_run.run_id,
            source_decision_run_id=source_decision.decision_run_id,
            evidence_identity=source_decision.evidence_identity,
            artifact_type=artifact_type,
            generator_name=generator_name,
            generator_version=generator_version,
            created_at=created_at,
            content_hash=content_hash,
            artifact_content=artifact_content,
            source_decision_provenance=source_decision,
            compared_decisions=compared_decisions,
            referenced_artifacts=referenced_artifacts,
        )
        return self._repository.publish(artifact)
