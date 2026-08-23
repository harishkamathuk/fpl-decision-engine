"""Generate immutable analytical artefacts downstream of completed decision runs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fpl_decision_engine.domain.analytical_artifact import (
    ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
    AnalyticalArtifact,
    AnalyticalArtifactType,
    calculate_analysis_artifact_id,
    calculate_analytical_content_hash,
)
from fpl_decision_engine.domain.run_record import RunRecord, RunState
from fpl_decision_engine.ports.analytical_artifacts import (
    AnalyticalArtifactError,
    AnalyticalArtifactRepository,
    AnalyticalContent,
    ComparisonArtifactGenerator,
    HistoryArtifactGenerator,
    PersistedAnalyticalArtifact,
    ReviewArtifactGenerator,
)


class AnalyticalArtifactService:
    """Build and publish derived outputs without owning or mutating run lifecycle."""

    def __init__(self, repository: AnalyticalArtifactRepository) -> None:
        self._repository = repository

    def generate_history(
        self,
        *,
        source_run: RunRecord,
        generator: HistoryArtifactGenerator,
        created_at: datetime,
        source_decision_run_id: UUID | None = None,
    ) -> PersistedAnalyticalArtifact:
        """Generate and publish history content for a completed run."""

        evidence_identity = self._validate_source_run(source_run)
        return self._publish(
            source_run=source_run,
            evidence_identity=evidence_identity,
            source_decision_run_id=source_decision_run_id,
            artifact_type=AnalyticalArtifactType.HISTORY,
            generator_name=generator.generator_name,
            generator_version=generator.generator_version,
            created_at=created_at,
            artifact_content=generator.generate_history(source_run=source_run),
        )

    def generate_comparison(
        self,
        *,
        source_run: RunRecord,
        source_decision_run_id: UUID,
        generator: ComparisonArtifactGenerator,
        created_at: datetime,
    ) -> PersistedAnalyticalArtifact:
        """Generate and publish comparison content for a completed run."""

        evidence_identity = self._validate_source_run(source_run)
        return self._publish(
            source_run=source_run,
            evidence_identity=evidence_identity,
            source_decision_run_id=source_decision_run_id,
            artifact_type=AnalyticalArtifactType.COMPARISON,
            generator_name=generator.generator_name,
            generator_version=generator.generator_version,
            created_at=created_at,
            artifact_content=generator.generate_comparison(source_run=source_run),
        )

    def generate_review(
        self,
        *,
        source_run: RunRecord,
        source_decision_run_id: UUID,
        generator: ReviewArtifactGenerator,
        created_at: datetime,
    ) -> PersistedAnalyticalArtifact:
        """Generate and publish review content for a completed run."""

        evidence_identity = self._validate_source_run(source_run)
        return self._publish(
            source_run=source_run,
            evidence_identity=evidence_identity,
            source_decision_run_id=source_decision_run_id,
            artifact_type=AnalyticalArtifactType.REVIEW,
            generator_name=generator.generator_name,
            generator_version=generator.generator_version,
            created_at=created_at,
            artifact_content=generator.generate_review(source_run=source_run),
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

    def _publish(
        self,
        *,
        source_run: RunRecord,
        evidence_identity: str,
        source_decision_run_id: UUID | None,
        artifact_type: AnalyticalArtifactType,
        generator_name: str,
        generator_version: str,
        created_at: datetime,
        artifact_content: AnalyticalContent,
    ) -> PersistedAnalyticalArtifact:
        content_hash = calculate_analytical_content_hash(artifact_content)
        artifact_id = calculate_analysis_artifact_id(
            schema_version=ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
            artifact_type=artifact_type,
            source_run_id=source_run.run_id,
            source_decision_run_id=source_decision_run_id,
            evidence_identity=evidence_identity,
            generator_name=generator_name,
            generator_version=generator_version,
            artifact_content=artifact_content,
        )
        artifact = AnalyticalArtifact(
            analysis_artifact_id=artifact_id,
            source_run_id=source_run.run_id,
            source_decision_run_id=source_decision_run_id,
            evidence_identity=evidence_identity,
            artifact_type=artifact_type,
            generator_name=generator_name,
            generator_version=generator_version,
            created_at=created_at,
            content_hash=content_hash,
            artifact_content=artifact_content,
        )
        return self._repository.publish(artifact)
