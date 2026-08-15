from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    AvailabilityReason,
    AvailabilityState,
    EvidenceConfidence,
)
from fpl_decision_engine.infrastructure.ingestion import prepare_snapshot
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import (
    FplSnapshotAvailabilityEvidenceProvider,
    map_snapshot,
)
from fpl_decision_engine.ports import (
    NewsEvidenceProvider,
    ProviderCapability,
    ProviderMappingError,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "fpl_snapshot"
PROCESSED_AT = datetime(2026, 8, 14, 12, 5, tzinfo=UTC)


def source_with_player_update(tmp_path: Path, update: dict[str, object]) -> Path:
    target = tmp_path / "input"
    shutil.copytree(FIXTURE_ROOT, target)
    path = target / "bootstrap-static.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["elements"][0].update(update)
    path.write_text(json.dumps(value), encoding="utf-8")
    return target


def provider_for(source: Path) -> FplSnapshotAvailabilityEvidenceProvider:
    snapshot = prepare_snapshot(source)
    canonical = map_snapshot(snapshot)
    return FplSnapshotAvailabilityEvidenceProvider(
        snapshot, canonical.players, processed_at=PROCESSED_AT
    )


@pytest.mark.parametrize(
    "update,state,reason,confidence",
    [
        (
            {"status": "a", "chance_of_playing_next_round": 100},
            AvailabilityState.AVAILABLE,
            AvailabilityReason.AVAILABLE,
            EvidenceConfidence.INDICATIVE,
        ),
        (
            {"status": "d", "chance_of_playing_this_round": 75},
            AvailabilityState.DOUBTFUL,
            AvailabilityReason.DOUBTFUL,
            EvidenceConfidence.AMBIGUOUS,
        ),
        (
            {"status": "i", "chance_of_playing_this_round": 0},
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.INJURY,
            EvidenceConfidence.DEFINITIVE,
        ),
        (
            {"status": "s"},
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.SUSPENSION,
            EvidenceConfidence.DEFINITIVE,
        ),
    ],
)
def test_structured_fpl_states_map_without_parsing_news(
    tmp_path: Path,
    update: dict[str, object],
    state: AvailabilityState,
    reason: AvailabilityReason,
    confidence: EvidenceConfidence,
) -> None:
    update["news"] = "free text is retained, not classified"
    response = provider_for(source_with_player_update(tmp_path, update)).evidence()
    item = next(record for record in response.data if record.source_external_player_id == "101")

    assert (item.state, item.reason, item.confidence) == (state, reason, confidence)
    assert item.source_text == "free text is retained, not classified"


def test_exact_identity_timestamps_and_snapshot_provenance_are_preserved(
    tmp_path: Path,
) -> None:
    published = "2026-08-14T11:30:00Z"
    source = source_with_player_update(
        tmp_path,
        {"status": "i", "news_added": published, "chance_of_playing_this_round": 0},
    )
    snapshot = prepare_snapshot(source)
    canonical = map_snapshot(snapshot)
    provider = FplSnapshotAvailabilityEvidenceProvider(
        snapshot, canonical.players, processed_at=PROCESSED_AT
    )

    response = provider.evidence((canonical.players[0].id,))
    item = response.data[0]

    assert isinstance(provider, NewsEvidenceProvider)
    assert provider.descriptor.supports(ProviderCapability.NEWS_EVIDENCE)
    assert item.player_id == canonical.players[0].id
    assert item.published_at == datetime(2026, 8, 14, 11, 30, tzinfo=UTC)
    assert item.observed_at == snapshot.observed_at
    assert item.processed_at == PROCESSED_AT
    assert response.provenance.snapshot_id == snapshot.expected_snapshot_id
    assert response.provenance.source_sha256 is not None
    assert response.provenance.mapping_fingerprint is not None
    assert response.freshness.as_of == snapshot.observed_at


def test_missing_news_added_is_not_replaced_with_observation_time(tmp_path: Path) -> None:
    response = provider_for(
        source_with_player_update(tmp_path, {"status": "d", "news_added": None})
    ).evidence()

    assert response.data[0].published_at is None


def test_unknown_or_ambiguous_exact_identity_fails(tmp_path: Path) -> None:
    snapshot = prepare_snapshot(source_with_player_update(tmp_path, {}))
    canonical = map_snapshot(snapshot)
    with pytest.raises(ProviderMappingError, match="not mapped"):
        FplSnapshotAvailabilityEvidenceProvider(
            snapshot, canonical.players[:1], processed_at=PROCESSED_AT
        )

    provider = FplSnapshotAvailabilityEvidenceProvider(
        snapshot, canonical.players, processed_at=PROCESSED_AT
    )
    with pytest.raises(ProviderMappingError, match="unknown canonical player"):
        provider.evidence((UUID(int=999),))


def test_not_selectable_and_removed_are_definitive_structured_states(
    tmp_path: Path,
) -> None:
    not_selectable = (
        provider_for(source_with_player_update(tmp_path, {"status": "a", "can_select": False}))
        .evidence()
        .data[0]
    )
    assert not_selectable.reason is AvailabilityReason.NOT_SELECTABLE
    assert not_selectable.confidence is EvidenceConfidence.DEFINITIVE

    second_root = tmp_path / "second"
    second_root.mkdir()
    removed = (
        provider_for(source_with_player_update(second_root, {"status": "a", "removed": True}))
        .evidence()
        .data[0]
    )
    assert removed.reason is AvailabilityReason.REMOVED
    assert removed.confidence is EvidenceConfidence.DEFINITIVE
