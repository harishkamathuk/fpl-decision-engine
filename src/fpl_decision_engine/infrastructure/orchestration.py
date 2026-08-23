"""Local adapters composing immutable evidence into the existing baseline path."""

from __future__ import annotations

from pathlib import Path

from fpl_decision_engine.application.decision_bundles import (
    build_decision_bundle,
    write_decision_bundle,
)
from fpl_decision_engine.application.gameweek_evidence import (
    GameweekEvidenceArtifact,
    parse_gameweek_evidence_manifest,
    validate_gameweek_evidence_references,
)
from fpl_decision_engine.application.orchestration import BaselineOutcome
from fpl_decision_engine.application.squad_runs import persist_squad_decision_run
from fpl_decision_engine.domain import (
    DecisionInputProvenance,
    GameweekNumber,
    SingleGameweekOptimisationRequest,
)
from fpl_decision_engine.domain.run_record import RunRecord
from fpl_decision_engine.infrastructure.ingestion.snapshots import (
    PreparedSnapshot,
    RawSourceObject,
)
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser
from fpl_decision_engine.infrastructure.persistence import DuckDbDecisionRunRepository
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import map_snapshot
from fpl_decision_engine.infrastructure.providers.projections import (
    FPL_FORECAST_PROVIDER_ID,
    FplForecastCsvAdapter,
)


class LocalBlankSquadBaselineRunner:
    """Run the existing unconstrained FPL Forecast blank-squad baseline.

    Component paths are taken only from the already-validated evidence manifest. The
    adapter reconstructs the canonical snapshot through the existing mapper, loads the
    projection CSV through the existing provider, invokes the unchanged HiGHS baseline,
    and persists the existing DecisionRun and content-addressed decision bundle.
    """

    def __init__(self, *, state_root: Path) -> None:
        self._state_root = state_root.resolve()

    def run(
        self,
        *,
        record: RunRecord,
        evidence_artifact: GameweekEvidenceArtifact,
    ) -> BaselineOutcome:
        manifest = parse_gameweek_evidence_manifest(evidence_artifact.read_bytes())
        validate_gameweek_evidence_references(
            manifest,
            lambda reference: Path(reference).read_bytes(),
            claimed_evidence_identity=record.evidence_identity,
        )
        if manifest.projection.provider_id != FPL_FORECAST_PROVIDER_ID:
            raise ValueError(
                "the bounded #84 baseline supports the existing FPL Forecast CSV adapter; "
                f"received provider {manifest.projection.provider_id!r}"
            )
        if record.code_revision is None or record.config_fingerprint is None:
            raise ValueError(
                "baseline execution requires recorded code_revision and config_fingerprint"
            )

        bootstrap = Path(manifest.snapshot.bootstrap.reference).read_bytes()
        fixtures = Path(manifest.snapshot.fixtures.reference).read_bytes()
        prepared = PreparedSnapshot(
            provider_id=manifest.snapshot.provider_id,
            observed_at=manifest.snapshot.observed_at,
            season=manifest.season,
            requested_snapshot_id=manifest.snapshot.snapshot_id,
            objects=(
                RawSourceObject(
                    resource_name="bootstrap-static",
                    original_filename=Path(manifest.snapshot.bootstrap.reference).name,
                    data=bootstrap,
                    sha256=manifest.snapshot.bootstrap.sha256,
                ),
                RawSourceObject(
                    resource_name="fixtures",
                    original_filename=Path(manifest.snapshot.fixtures.reference).name,
                    data=fixtures,
                    sha256=manifest.snapshot.fixtures.sha256,
                ),
            ),
        )
        canonical = map_snapshot(prepared)
        gameweek = GameweekNumber(value=record.gameweek)
        projection_provider = FplForecastCsvAdapter(
            Path(manifest.projection.artifact.reference),
            canonical.players,
            season=record.season,
            observed_at=manifest.acquisition.projection_acquired_at,
        )
        projections = projection_provider.projections((gameweek,)).data
        projected_player_ids = {projection.player_id for projection in projections}
        players = tuple(
            player for player in canonical.players if player.id in projected_player_ids
        )
        request = SingleGameweekOptimisationRequest(
            target_gameweek=gameweek,
            players=players,
            projections=projections,
        )
        result = HighsSingleGameweekOptimiser().optimise(request)
        inputs = DecisionInputProvenance(
            official_snapshot_reference=manifest.snapshot.source_reference,
            official_snapshot_id=manifest.snapshot.snapshot_id,
            official_snapshot_sha256=manifest.snapshot.content_sha256,
            projection_provider=manifest.projection.provider_id,
            projection_source=manifest.projection.source,
            projection_artifact_reference=manifest.projection.artifact.reference,
            projection_sha256=manifest.projection.artifact.sha256,
            projection_model_version=manifest.projection.model_version,
            projection_generated_at=manifest.projection.generated_at,
        )
        bundle = build_decision_bundle(
            run_id=record.run_id,
            decision_at=record.created_at,
            season=record.season,
            code_revision=record.code_revision,
            config_fingerprint=record.config_fingerprint,
            inputs=inputs,
            request=request,
            result=result,
        )
        bundle_artifact = write_decision_bundle(bundle, state_root=self._state_root)
        persist_squad_decision_run(
            DuckDbDecisionRunRepository(self._state_root / "fpl.duckdb"),
            run_id=record.run_id,
            created_at=record.created_at,
            season=record.season,
            code_revision=record.code_revision,
            source_is_dirty=None,
            config_fingerprint=record.config_fingerprint,
            input_snapshot_references=(manifest.snapshot.source_reference,),
            request=request,
            result=result,
            output_artifact_reference=bundle_artifact.reference,
        )
        summary = (
            f"objective={bundle.recommendation.primary_objective:.6f}; "
            f"decision={bundle_artifact.reference}"
        )
        return BaselineOutcome(
            recommendation=bundle.recommendation,
            reference=bundle_artifact.reference,
            sha256=bundle_artifact.sha256,
            summary=summary,
        )
