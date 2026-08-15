"""Deterministic local-file adapter for FPL-shaped manager state."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fpl_decision_engine.domain import (
    GameweekNumber,
    ManagerState,
    Money,
    Player,
    Squad,
    SquadMember,
    calculate_selling_price,
)
from fpl_decision_engine.ports import (
    Freshness,
    ProviderCapability,
    ProviderDataError,
    ProviderDescriptor,
    ProviderMappingError,
    ProviderProvenance,
    ProviderResponse,
)

PROVIDER_ID = "fpl_local_manager_state"
_MANAGER_NAMESPACE = UUID("47999bb6-392f-5b1f-98a8-674405714395")


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class _SourcePick(_SourceModel):
    element: int = Field(gt=0)
    purchase_price: int = Field(ge=0)
    selling_price: int = Field(ge=0)


class _SourceTransferState(_SourceModel):
    bank: int = Field(ge=0)
    limit: int = Field(ge=1, le=5)
    made: int = Field(default=0, ge=0)
    cost: int = Field(default=0, ge=0)
    active_chip: str | None = None


class _SourceManagerState(_SourceModel):
    manager_id: str | int
    gameweek: int = Field(ge=1, le=38)
    picks: tuple[_SourcePick, ...]
    transfers: _SourceTransferState


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class LocalFplManagerStateProvider:
    """Map one supplied FPL-shaped manager-team file through exact element IDs.

    Source prices and bank are integer tenths of a million pounds. ``limit`` is the
    free-transfer allowance before the source payload's already-made transfers;
    canonical ``free_transfers`` stores ``max(limit - made, 0)``. Existing ``cost`` is
    retained as sunk history and is not charged to a new recommendation.
    """

    def __init__(
        self,
        path: Path,
        players: Sequence[Player],
        *,
        identity_namespace: str,
        season: str,
        observed_at: datetime,
        provider_version: str = "1",
    ) -> None:
        _require_aware(observed_at, "observed_at")
        self._path = path.resolve()
        try:
            source_bytes = self._path.read_bytes()
            source = _SourceManagerState.model_validate_json(source_bytes)
        except OSError as exc:
            raise ProviderDataError(
                f"cannot read local manager state: {self._path}", provider_id=PROVIDER_ID
            ) from exc
        except ValidationError as exc:
            raise ProviderDataError(
                f"invalid FPL-shaped manager state: {exc}", provider_id=PROVIDER_ID
            ) from exc
        if source.transfers.active_chip is not None:
            raise ProviderDataError(
                "chip-active or unlimited manager state is unsupported in Issue #7",
                provider_id=PROVIDER_ID,
            )

        players_by_external: dict[str, Player] = {}
        for player in players:
            refs = [
                ref.external_id
                for ref in player.external_refs
                if ref.provider == identity_namespace
            ]
            if len(refs) > 1:
                raise ProviderMappingError(
                    f"canonical player {player.id} has ambiguous {identity_namespace} references",
                    provider_id=PROVIDER_ID,
                )
            if refs:
                if refs[0] in players_by_external:
                    raise ProviderMappingError(
                        f"ambiguous canonical mapping for manager element {refs[0]}",
                        provider_id=PROVIDER_ID,
                    )
                players_by_external[refs[0]] = player

        source_elements = [str(pick.element) for pick in source.picks]
        if len(source_elements) != len(set(source_elements)):
            raise ProviderDataError(
                "manager state contains duplicate element picks", provider_id=PROVIDER_ID
            )
        missing = sorted(set(source_elements) - players_by_external.keys())
        if missing:
            raise ProviderMappingError(
                f"manager element {missing[0]} is not mapped to a canonical player",
                provider_id=PROVIDER_ID,
            )

        members: list[SquadMember] = []
        for pick in source.picks:
            player = players_by_external[str(pick.element)]
            purchase = Money(tenths_million=pick.purchase_price)
            selling = Money(tenths_million=pick.selling_price)
            expected_selling = calculate_selling_price(purchase=purchase, current=player.price)
            if selling != expected_selling:
                raise ProviderDataError(
                    f"manager element {pick.element} selling price {pick.selling_price} "
                    f"does not match official rule value {expected_selling.tenths_million}",
                    provider_id=PROVIDER_ID,
                )
            members.append(
                SquadMember(
                    player_id=player.id,
                    team_id=player.team_id,
                    position=player.position,
                    purchase_price=purchase,
                    selling_price=selling,
                )
            )

        try:
            squad = Squad(members=tuple(members))
        except ValueError as exc:
            raise ProviderDataError(
                f"manager picks do not form a legal canonical squad: {exc}",
                provider_id=PROVIDER_ID,
            ) from exc

        manager_reference = str(source.manager_id)
        self._state = ManagerState(
            manager_id=uuid5(_MANAGER_NAMESPACE, f"{PROVIDER_ID}:{manager_reference}"),
            gameweek=GameweekNumber(value=source.gameweek),
            squad=squad,
            bank=Money(tenths_million=source.transfers.bank),
            free_transfers=max(source.transfers.limit - source.transfers.made, 0),
            transfers_made=source.transfers.made,
            existing_points_cost=source.transfers.cost,
        )
        mapping = "\n".join(
            f"{element}:{players_by_external[element].id}"
            for element in sorted(source_elements, key=int)
        )
        self._mapping_fingerprint = hashlib.sha256(mapping.encode()).hexdigest()
        self._source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        self._manager_reference = manager_reference
        self._season = season
        self._observed_at = observed_at
        self._descriptor = ProviderDescriptor(
            provider_id=PROVIDER_ID,
            display_name="Local FPL-shaped manager state",
            version=provider_version,
            capabilities=frozenset({ProviderCapability.MANAGER_STATE}),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def manager_state(
        self,
        manager_reference: str,
        gameweek: GameweekNumber,
    ) -> ProviderResponse[ManagerState]:
        if manager_reference != self._manager_reference:
            raise ProviderMappingError(
                f"manager reference {manager_reference!r} does not match local payload",
                provider_id=PROVIDER_ID,
            )
        if gameweek != self._state.gameweek:
            raise ProviderDataError(
                f"manager payload gameweek {self._state.gameweek.value} does not match "
                f"requested gameweek {gameweek.value}",
                provider_id=PROVIDER_ID,
            )
        return ProviderResponse(
            data=self._state,
            provenance=ProviderProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.version,
                retrieved_at=self._observed_at,
                source_reference=str(self._path),
                snapshot_id=f"sha256:{self._source_sha256}",
                source_sha256=self._source_sha256,
                mapping_fingerprint=self._mapping_fingerprint,
                season=self._season,
            ),
            freshness=Freshness(as_of=self._observed_at),
        )
