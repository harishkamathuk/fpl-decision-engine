"""Downstream history/comparison integration for completed baseline runs (Issue #85).

This seam runs strictly after a baseline ``RunRecord`` is already completed. Previous
state is resolved exclusively from ``current_run.previous_run_id``: when it is absent
only history is generated and no CHANGED/UNCHANGED classification is invented; when it
is present the exact recorded previous run is loaded and its immutable decision bundle
is compared against the current bundle by ``recommendation.identity`` only. Artefact
hashes are provenance, not the changed/unchanged predicate.

The seam never mutates run lifecycle or decision provenance: analytical failures are
surfaced as explicit ``AnalyticalHistoryError`` values for the caller to report as
analytical WARN/error output, and a completed run remains completed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from fpl_decision_engine.application.analytical_artifacts import AnalyticalArtifactService
from fpl_decision_engine.application.decision_bundles import DecisionBundleError
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain.decision_bundle import (
    DecisionBundleV1,
    DecisionRecommendation,
)
from fpl_decision_engine.domain.provenance import DecisionProvenance
from fpl_decision_engine.domain.run_record import (
    LegacyRunRecord,
    RecordedDecision,
    RunRecord,
    RunState,
)
from fpl_decision_engine.ports.analytical_artifacts import (
    AnalyticalArtifactError,
    AnalyticalArtifactGenerator,
    AnalyticalContent,
    ComparisonGeneratorInput,
    GeneratorInputData,
    HistoryGeneratorInput,
    PersistedAnalyticalArtifact,
)
from fpl_decision_engine.ports.run_records import RunRecordError


class RecommendationChange(StrEnum):
    """Approved #85 changed/unchanged classification of recommendation identity."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"


class AnalyticalHistoryError(AnalyticalArtifactError):
    """The downstream analytical integration could not resolve or publish its inputs."""


@dataclass(frozen=True, slots=True)
class AnalyticalHistoryResult:
    """Immutable downstream outputs for one completed run and its explicit lineage."""

    history: PersistedAnalyticalArtifact
    comparison: PersistedAnalyticalArtifact | None
    change: RecommendationChange | None


class HistoryGenerator:
    """Publish the preloaded history values as the immutable history artefact content."""

    def generate(self, *, generator_input: HistoryGeneratorInput) -> AnalyticalContent:
        return generator_input.history_inputs.as_content()


class ComparisonGenerator:
    """Publish the preloaded comparison values as the immutable comparison content."""

    def generate(self, *, generator_input: ComparisonGeneratorInput) -> AnalyticalContent:
        return generator_input.comparison_inputs.as_content()


def classify_recommendation_change(
    *,
    previous_identity: tuple[object, ...],
    current_identity: tuple[object, ...],
) -> RecommendationChange:
    """Classify by recommendation identity; artefact hash equality is not the predicate."""

    return (
        RecommendationChange.UNCHANGED
        if previous_identity == current_identity
        else RecommendationChange.CHANGED
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity_payload(recommendation: DecisionRecommendation) -> AnalyticalContent:
    """Serialize one recommendation identity into canonical JSON values."""

    return {
        "squad_ids": [str(value) for value in recommendation.squad_ids],
        "starting_xi_ids": [str(value) for value in recommendation.starting_xi_ids],
        "captain_id": str(recommendation.captain_id),
        "vice_captain_id": str(recommendation.vice_captain_id),
        "bench_ids": [str(value) for value in recommendation.bench_ids],
    }


def _history_content(
    *,
    run: RunRecord,
    provenance: DecisionProvenance,
    bundle: DecisionBundleV1,
) -> AnalyticalContent:
    return {
        "source_run_id": str(run.run_id),
        "source_decision_run_id": str(provenance.decision_run_id),
        "evidence_identity": provenance.evidence_identity,
        "season": run.season,
        "gameweek": run.gameweek,
        "decision_at": _timestamp(bundle.decision_at),
        "decision_artifact_hash": provenance.decision_artifact_hash,
        "recommendation_identity": _identity_payload(bundle.recommendation),
    }


def _comparison_content(
    *,
    current_run: RunRecord,
    current_provenance: DecisionProvenance,
    current_bundle: DecisionBundleV1,
    previous_run: RunRecord,
    previous_provenance: DecisionProvenance,
    previous_bundle: DecisionBundleV1,
    change: RecommendationChange,
) -> AnalyticalContent:
    return {
        "current_run_id": str(current_run.run_id),
        "previous_run_id": str(previous_run.run_id),
        "current_decision_artifact_hash": current_provenance.decision_artifact_hash,
        "previous_decision_artifact_hash": previous_provenance.decision_artifact_hash,
        "current_recommendation_identity": _identity_payload(current_bundle.recommendation),
        "previous_recommendation_identity": _identity_payload(previous_bundle.recommendation),
        "classification": change.value,
    }


class AnalyticalHistoryService:
    """Generate history/comparison artefacts for a completed run's explicit lineage.

    ``bundle_loader`` is the existing immutable decision-bundle read seam: it resolves
    the exact persisted bytes through the recorded content-addressed reference and
    hash. Generators and persistence run behind the existing
    ``AnalyticalArtifactService``; this seam only resolves preloaded values and never
    touches run lifecycle or decision provenance.
    """

    def __init__(
        self,
        *,
        records: RunRecordService,
        bundle_loader: Callable[..., DecisionBundleV1],
        analytical: AnalyticalArtifactService,
        history_generator: AnalyticalArtifactGenerator[HistoryGeneratorInput] | None = None,
        comparison_generator: AnalyticalArtifactGenerator[ComparisonGeneratorInput] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._records = records
        self._bundle_loader = bundle_loader
        self._analytical = analytical
        self._history_generator = history_generator or HistoryGenerator()
        self._comparison_generator = comparison_generator or ComparisonGenerator()
        self._now = now or (lambda: datetime.now(UTC))

    def generate(self, *, run_id: UUID) -> AnalyticalHistoryResult:
        """Resolve the completed run's explicit lineage and publish derived artefacts.

        Previous state is never guessed: ``previous_run_id`` is followed exactly, and
        any missing or invalid previous state fails explicitly before anything is
        published, leaving the completed current run untouched.
        """

        current = self._load_typed_run(run_id, "current")
        self._require_completed(current)
        current_decision = self._recorded_decision(current, "current")
        current_bundle = self._load_bundle(current_decision, "current")
        previous = self._resolve_previous(current)

        current_provenance = current_decision.provenance
        history_input = HistoryGeneratorInput(
            source_decision=current_provenance,
            generator_name="decision-history",
            generator_version="1.0.0",
            history_inputs=GeneratorInputData.from_content(
                _history_content(
                    run=current,
                    provenance=current_provenance,
                    bundle=current_bundle,
                )
            ),
        )
        history = self._analytical.generate_history(
            source_run=current,
            generator_input=history_input,
            generator=self._history_generator,
            created_at=self._now(),
        )
        if previous is None:
            return AnalyticalHistoryResult(history=history, comparison=None, change=None)

        previous_record, previous_decision, previous_bundle = previous
        previous_provenance = previous_decision.provenance
        change = classify_recommendation_change(
            previous_identity=previous_bundle.recommendation.identity,
            current_identity=current_bundle.recommendation.identity,
        )
        comparison_input = ComparisonGeneratorInput(
            source_decision=current_provenance,
            compared_decisions=(previous_provenance,),
            generator_name="decision-comparison",
            generator_version="1.0.0",
            comparison_inputs=GeneratorInputData.from_content(
                _comparison_content(
                    current_run=current,
                    current_provenance=current_provenance,
                    current_bundle=current_bundle,
                    previous_run=previous_record,
                    previous_provenance=previous_provenance,
                    previous_bundle=previous_bundle,
                    change=change,
                )
            ),
        )
        comparison = self._analytical.generate_comparison(
            source_run=current,
            generator_input=comparison_input,
            generator=self._comparison_generator,
            created_at=self._now(),
        )
        return AnalyticalHistoryResult(
            history=history,
            comparison=comparison,
            change=change,
        )

    def _load_typed_run(self, run_id: UUID, label: str) -> RunRecord:
        try:
            record = self._records.get_run(run_id)
        except RunRecordError as exc:
            raise AnalyticalHistoryError(
                f"{label} run {run_id} cannot be loaded: {exc}"
            ) from exc
        if isinstance(record, LegacyRunRecord):
            raise AnalyticalHistoryError(
                f"{label} run {run_id} is a legacy record without a typed schema; "
                "analytical history does not fabricate previous state"
            )
        return record

    def _require_completed(self, record: RunRecord) -> None:
        if record.state not in (RunState.COMPLETED, RunState.AUTHORITATIVE):
            raise AnalyticalHistoryError(
                f"run {record.run_id} is {record.state.value}; history/comparison "
                "requires an already completed baseline run"
            )

    def _resolve_previous(
        self,
        current: RunRecord,
    ) -> tuple[RunRecord, RecordedDecision, DecisionBundleV1] | None:
        previous_run_id = current.previous_run_id
        if previous_run_id is None:
            return None
        previous = self._load_typed_run(previous_run_id, "previous")
        previous_decision = self._recorded_decision(previous, "previous")
        previous_bundle = self._load_bundle(previous_decision, "previous")
        return previous, previous_decision, previous_bundle

    def _recorded_decision(self, record: RunRecord, label: str) -> RecordedDecision:
        decision = next(
            (
                item
                for item in reversed(record.decisions)
                if isinstance(item, RecordedDecision)
            ),
            None,
        )
        if decision is None:
            raise AnalyticalHistoryError(
                f"{label} run {record.run_id} has no recorded decision provenance"
            )
        return decision

    def _load_bundle(self, decision: RecordedDecision, label: str) -> DecisionBundleV1:
        try:
            bundle = self._bundle_loader(
                reference=decision.reference, sha256=decision.sha256
            )
        except (DecisionBundleError, OSError, ValueError) as exc:
            raise AnalyticalHistoryError(
                f"{label} decision bundle {decision.reference!r} for run "
                f"{decision.provenance.run_id} cannot be loaded: {exc}"
            ) from exc
        if bundle.decision_run_id != decision.provenance.decision_run_id:
            raise AnalyticalHistoryError(
                f"{label} decision bundle {decision.reference!r} asserts decision_run_id "
                f"{bundle.decision_run_id}, but provenance records "
                f"{decision.provenance.decision_run_id}"
            )
        return bundle
