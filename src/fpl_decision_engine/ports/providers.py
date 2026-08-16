"""Provider ports for external FPL data and analytical inputs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from fpl_decision_engine.domain import (
    Fixture,
    Gameweek,
    GameweekNumber,
    League,
    ManagerState,
    Player,
    Projection,
    Team,
)

from .types import ProviderDescriptor, ProviderResponse


@runtime_checkable
class PlayerDataProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def teams(self) -> ProviderResponse[tuple[Team, ...]]: ...

    def players(self) -> ProviderResponse[tuple[Player, ...]]: ...

    def gameweeks(self) -> ProviderResponse[tuple[Gameweek, ...]]: ...

    def fixtures(
        self,
        gameweeks: Sequence[GameweekNumber] | None = None,
    ) -> ProviderResponse[tuple[Fixture, ...]]: ...


@runtime_checkable
class ManagerStateProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def manager_state(
        self,
        manager_reference: str,
        gameweek: GameweekNumber,
    ) -> ProviderResponse[ManagerState]: ...


@runtime_checkable
class LeagueProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def league(self, league_reference: str) -> ProviderResponse[League]: ...


@runtime_checkable
class ProjectionProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def projections(
        self,
        gameweeks: Sequence[GameweekNumber],
    ) -> ProviderResponse[tuple[Projection, ...]]: ...


@runtime_checkable
class NewsEvidenceProvider[EvidenceT](Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def evidence(
        self,
        player_ids: Sequence[UUID] | None = None,
    ) -> ProviderResponse[tuple[EvidenceT, ...]]: ...
