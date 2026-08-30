"""Acceptance tests for application construction of lineup observations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from fpl_decision_engine.application import build_lineup_evidence_observation
from fpl_decision_engine.domain import (
    GameweekNumber,
    LineupEvidenceClass,
    LineupEvidenceProvenance,
    LineupEvidenceStatus,
    Projection,
)
from fpl_decision_engine.ports import ProviderProvenance

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
PLAYER_ID = UUID(int=92_101)


def make_projection() -> Projection:
    return Projection(
        player_id=PLAYER_ID,
        gameweek=GameweekNumber(value=7),
        expected_points=8.125,
        expected_minutes=91.5,
        appearance_probability=0.96,
        start_probability=0.6125,
        source="projection-provider",
        model_version="projection-model-v2",
        generated_at=NOW,
    )


def make_evidence() -> LineupEvidenceProvenance:
    return LineupEvidenceProvenance(
        provider_id="lineup-provider",
        provider_version="lineup-v3",
        source_reference="fixture://lineup.json",
        snapshot_id="lineup-snapshot",
        evidence_ids=("lineup:1",),
        raw_sha256="a" * 64,
        mapping_fingerprint="b" * 64,
        observed_at=NOW,
        retrieved_at=NOW,
    )


def make_provenance(**updates: object) -> ProviderProvenance:
    values: dict[str, object] = {
        "provider_id": "projection-provider",
        "provider_version": "projection-v4",
        "retrieved_at": NOW,
        "source_reference": "fixture://projection.csv",
        "snapshot_id": "projection-snapshot",
        "source_sha256": "c" * 64,
        "mapping_fingerprint": "d" * 64,
        "season": "2026-27",
    }
    values.update(updates)
    return ProviderProvenance(**values)


def test_application_extracts_provenance_and_preserves_frozen_projection() -> None:
    projection = make_projection()
    before = projection.model_copy(deep=True)

    observation = build_lineup_evidence_observation(
        season="2026-27",
        projection=projection,
        projection_provenance=make_provenance(),
        evidence_status=LineupEvidenceStatus.CLASSIFIED,
        evidence_class=LineupEvidenceClass.NO_MATERIAL_SIGNAL,
        evidence=make_evidence(),
    )

    assert observation.season == "2026-27"
    assert observation.gameweek == projection.gameweek
    assert observation.canonical_player_id == projection.player_id
    assert observation.original_p_start == projection.start_probability
    assert observation.projection_generated_at == projection.generated_at
    assert observation.projection_provider_id == "projection-provider"
    assert observation.projection_provider_version == "projection-v4"
    assert observation.projection_source_reference == "fixture://projection.csv"
    assert observation.projection_source_sha256 == "c" * 64
    assert observation.projection_snapshot_id == "projection-snapshot"
    assert observation.projection_mapping_fingerprint == "d" * 64
    assert observation.evidence == make_evidence()
    assert projection == before
    assert projection.expected_points == 8.125
    assert projection.expected_minutes == 91.5
    assert projection.appearance_probability == 0.96
    assert projection.start_probability == 0.6125


@pytest.mark.parametrize(
    ("provenance_updates", "message"),
    [
        ({"provider_id": "different-provider"}, "provider identity"),
        ({"season": "2025-26"}, "season"),
    ],
)
def test_application_rejects_projection_provenance_mismatch(
    provenance_updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_lineup_evidence_observation(
            season="2026-27",
            projection=make_projection(),
            projection_provenance=make_provenance(**provenance_updates),
            evidence_status=LineupEvidenceStatus.MISSING,
            evidence_class=None,
            evidence=make_evidence(),
        )
