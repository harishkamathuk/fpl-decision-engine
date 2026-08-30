"""Application construction for immutable lineup-evidence observations."""

from fpl_decision_engine.domain import (
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    LineupEvidenceValidationObservation,
    Projection,
)
from fpl_decision_engine.ports.types import ProviderProvenance


def build_lineup_evidence_observation(
    *,
    season: str,
    projection: Projection,
    projection_provenance: ProviderProvenance,
    evidence_status: LineupEvidenceStatus,
    evidence_class: LineupEvidenceClass | None,
    evidence: LineupEvidenceProvenance,
) -> LineupEvidenceValidationObservation:
    """Construct an observation from frozen forecast and already-classified evidence."""

    if projection_provenance.season is not None and projection_provenance.season != season:
        raise ValueError("projection provenance season does not match observation")
    if projection_provenance.provider_id != projection.source:
        raise ValueError("projection provider identity does not match projection source")
    return LineupEvidenceValidationObservation.from_projection(
        season=season,
        projection=projection,
        projection_provider_version=projection_provenance.provider_version,
        projection_source_reference=projection_provenance.source_reference,
        projection_source_sha256=projection_provenance.source_sha256,
        projection_snapshot_id=projection_provenance.snapshot_id,
        projection_mapping_fingerprint=projection_provenance.mapping_fingerprint,
        evidence_status=evidence_status,
        evidence_class=evidence_class,
        evidence=evidence,
    )
