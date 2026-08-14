"""Provider-independent orchestration for a core FPL data sync."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fpl_decision_engine.ports import (
    PlayerDataProvider,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderResponse,
)


@dataclass(frozen=True, slots=True)
class SyncResult:
    snapshot_id: str
    provider_id: str
    observed_at: datetime
    age: timedelta
    gameweek_count: int
    team_count: int
    player_count: int
    fixture_count: int
    warnings: tuple[str, ...]
    evidence_location: Path
    created: bool


def _snapshot_id[T](response: ProviderResponse[T], provider_id: str) -> str:
    snapshot_id = response.provenance.snapshot_id
    if snapshot_id is None:
        raise ProviderCapabilityError(
            "sync provider response does not identify its source snapshot",
            provider_id=provider_id,
        )
    return snapshot_id


def sync_data(
    provider: PlayerDataProvider,
    *,
    now: datetime,
    evidence_location: Path,
    warnings: tuple[str, ...] = (),
    created: bool,
) -> SyncResult:
    """Read a complete canonical dataset through the player-data port."""

    required_capabilities = (
        ProviderCapability.PLAYER_DATA,
        ProviderCapability.FIXTURE_DATA,
        ProviderCapability.GAMEWEEK_DATA,
    )
    for capability in required_capabilities:
        if not provider.descriptor.supports(capability):
            raise ProviderCapabilityError(
                f"provider does not support required capability: {capability}",
                provider_id=provider.descriptor.provider_id,
            )

    teams = provider.teams()
    players = provider.players()
    gameweeks = provider.gameweeks()
    fixtures = provider.fixtures()
    responses = (teams, players, gameweeks, fixtures)
    snapshot_ids = {
        _snapshot_id(response, provider.descriptor.provider_id) for response in responses
    }
    if len(snapshot_ids) != 1:
        raise ProviderCapabilityError(
            "provider returned data from inconsistent source snapshots",
            provider_id=provider.descriptor.provider_id,
        )
    observed_times = {response.freshness.as_of for response in responses}
    if len(observed_times) != 1:
        raise ProviderCapabilityError(
            "provider returned inconsistent freshness metadata",
            provider_id=provider.descriptor.provider_id,
        )
    observed_at = observed_times.pop()
    age = max(now - observed_at, timedelta(0))
    return SyncResult(
        snapshot_id=snapshot_ids.pop(),
        provider_id=provider.descriptor.provider_id,
        observed_at=observed_at,
        age=age,
        gameweek_count=len(gameweeks.data),
        team_count=len(teams.data),
        player_count=len(players.data),
        fixture_count=len(fixtures.data),
        warnings=warnings,
        evidence_location=evidence_location,
        created=created,
    )
