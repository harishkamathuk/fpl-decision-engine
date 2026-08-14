"""Player-data provider backed by one already validated local snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from fpl_decision_engine.domain import Fixture, Gameweek, GameweekNumber, Player, Team
from fpl_decision_engine.ports import (
    Freshness,
    ProviderCapability,
    ProviderDescriptor,
    ProviderProvenance,
    ProviderResponse,
)

from .adapter import CanonicalFplSnapshot


class FplSnapshotProvider:
    """Expose canonical data from an immutable FPL-shaped snapshot."""

    def __init__(
        self,
        data: CanonicalFplSnapshot,
        *,
        provider_id: str,
        snapshot_id: str,
        observed_at: datetime,
        imported_at: datetime,
        source_reference: str,
    ) -> None:
        self._data = data
        self._snapshot_id = snapshot_id
        self._observed_at = observed_at
        self._imported_at = imported_at
        self._source_reference = source_reference
        self._descriptor = ProviderDescriptor(
            provider_id=provider_id,
            display_name="FPL-shaped local snapshot",
            version="1",
            capabilities=frozenset(
                {
                    ProviderCapability.PLAYER_DATA,
                    ProviderCapability.FIXTURE_DATA,
                    ProviderCapability.GAMEWEEK_DATA,
                }
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._data.warnings

    def _response[T](self, data: T) -> ProviderResponse[T]:
        return ProviderResponse(
            data=data,
            provenance=ProviderProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.version,
                retrieved_at=self._imported_at,
                source_reference=self._source_reference,
                snapshot_id=self._snapshot_id,
            ),
            freshness=Freshness(as_of=self._observed_at),
        )

    def teams(self) -> ProviderResponse[tuple[Team, ...]]:
        return self._response(self._data.teams)

    def players(self) -> ProviderResponse[tuple[Player, ...]]:
        return self._response(self._data.players)

    def gameweeks(self) -> ProviderResponse[tuple[Gameweek, ...]]:
        return self._response(self._data.gameweeks)

    def fixtures(
        self,
        gameweeks: Sequence[GameweekNumber] | None = None,
    ) -> ProviderResponse[tuple[Fixture, ...]]:
        if gameweeks is None:
            return self._response(self._data.fixtures)
        requested = {gameweek.value for gameweek in gameweeks}
        return self._response(
            tuple(
                fixture
                for fixture in self._data.fixtures
                if fixture.gameweek is not None and fixture.gameweek.value in requested
            )
        )
