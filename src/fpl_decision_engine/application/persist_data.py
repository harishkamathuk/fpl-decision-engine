"""Application orchestration for persisting one provider snapshot canonically."""

from __future__ import annotations

from datetime import datetime

from fpl_decision_engine.ports import PlayerDataProvider, ProviderCapabilityError
from fpl_decision_engine.ports.persistence import (
    CanonicalRepository,
    CanonicalSnapshot,
    SnapshotRegistration,
    SourceObjectHash,
)


def persist_provider_snapshot(
    provider: PlayerDataProvider,
    repository: CanonicalRepository,
    *,
    season: str,
    processed_at: datetime,
    source_hashes: tuple[SourceObjectHash, ...] = (),
    source_reference: str | None = None,
    published_at: datetime | None = None,
    code_revision: str | None = None,
) -> SnapshotRegistration:
    """Persist a consistent provider snapshot without depending on storage technology."""

    teams = provider.teams()
    players = provider.players()
    gameweeks = provider.gameweeks()
    fixtures = provider.fixtures()
    responses = (teams, players, gameweeks, fixtures)
    snapshot_ids = {response.provenance.snapshot_id for response in responses}
    if None in snapshot_ids or len(snapshot_ids) != 1:
        raise ProviderCapabilityError(
            "provider responses must identify one consistent source snapshot",
            provider_id=provider.descriptor.provider_id,
        )
    observed_times = {response.freshness.as_of for response in responses}
    if len(observed_times) != 1:
        raise ProviderCapabilityError(
            "provider responses must use one consistent observed_at timestamp",
            provider_id=provider.descriptor.provider_id,
        )
    snapshot_id = snapshot_ids.pop()
    assert snapshot_id is not None
    return repository.save(
        CanonicalSnapshot(
            provider_id=provider.descriptor.provider_id,
            season=season,
            snapshot_id=snapshot_id,
            observed_at=observed_times.pop(),
            processed_at=processed_at,
            published_at=published_at,
            source_reference=source_reference,
            code_revision=code_revision,
            source_hashes=source_hashes,
            teams=teams.data,
            players=players.data,
            gameweeks=gameweeks.data,
            fixtures=fixtures.data,
        )
    )
