"""Authenticated official FPL API adapter for operational manager-state acquisition."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from fpl_decision_engine.application.manager_state import ManagerStateSource
from fpl_decision_engine.domain import GameweekNumber, Position
from fpl_decision_engine.domain.manager_state import ManagerStateSnapshot, RawManagerPick
from fpl_decision_engine.ports.errors import (
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderManagerIdentityError,
    ProviderUnavailableError,
)

PROVIDER_ID = "official_fpl_api"


class AuthenticatedHttp(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> Any: ...


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Me(_SourceModel):
    player_id: int = Field(gt=0)


class _Pick(_SourceModel):
    element: int = Field(gt=0)
    position: int = Field(gt=0, le=15)
    is_captain: bool = False
    is_vice_captain: bool = False


class _Team(_SourceModel):
    id: int = Field(gt=0)
    picks: tuple[_Pick, ...] = Field(min_length=15, max_length=15)
    event: int = Field(ge=1, le=38)
    last_deadline_bank: int | None = None
    last_deadline_value: int | None = None
    transfers: dict[str, Any] = {}


class _Event(_SourceModel):
    id: int = Field(ge=1, le=38)
    deadline_time: AwareDatetime


class _Element(_SourceModel):
    id: int = Field(gt=0)
    element_type: int = Field(ge=1, le=4)


class _Bootstrap(_SourceModel):
    events: tuple[_Event, ...]
    elements: tuple[_Element, ...]


class OfficialFplManagerStateSource(ManagerStateSource):
    """Fetch `/api/me/`, `/api/my-team/{entry_id}/` and bootstrap metadata."""

    def __init__(
        self,
        http: AuthenticatedHttp,
        *,
        base_url: str,
        headers: Mapping[str, str],
        timeout: float = 20.0,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self._timeout = timeout

    def _get(self, endpoint: str) -> object:
        try:
            response = self._http.get(
                f"{self._base_url}{endpoint}",
                headers=self._headers,
                timeout=self._timeout,
            )
            status = getattr(response, "status_code", 200)
            if status in (401, 403):
                raise ProviderAuthenticationError(
                    "official FPL authentication rejected", provider_id=PROVIDER_ID
                )
            if status >= 400:
                raise ProviderUnavailableError(
                    "official FPL endpoint unavailable", provider_id=PROVIDER_ID
                )
            return response.json()
        except (ProviderAuthenticationError, ProviderUnavailableError):
            raise
        except Exception as exc:
            raise ProviderUnavailableError(
                "official FPL endpoint unavailable", provider_id=PROVIDER_ID
            ) from exc

    def acquire(self, *, entry_id: int, target_event: GameweekNumber) -> ManagerStateSnapshot:
        """Acquire and normalize one live state without retaining authentication material."""
        try:
            me = _Me.model_validate(self._get("/api/me/"))
            if me.player_id != entry_id:
                raise ProviderManagerIdentityError(
                    "authenticated FPL entry does not match configured entry",
                    provider_id=PROVIDER_ID,
                )
            team = _Team.model_validate(self._get(f"/api/my-team/{entry_id}/"))
            bootstrap = _Bootstrap.model_validate(self._get("/api/bootstrap-static/"))
        except ProviderDataError:
            raise
        except ValidationError as exc:
            raise ProviderDataError(
                "official FPL response is malformed", provider_id=PROVIDER_ID
            ) from exc
        if team.id != entry_id:
            raise ProviderManagerIdentityError(
                "official FPL manager identity mismatch", provider_id=PROVIDER_ID
            )
        event = next((item for item in bootstrap.events if item.id == target_event.value), None)
        if event is None or team.event != target_event.value:
            raise ProviderDataError(
                "official FPL target event is inconsistent", provider_id=PROVIDER_ID
            )
        types = {
            item.id: Position(
                {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}[item.element_type]
            )
            for item in bootstrap.elements
        }
        if any(p.element not in types for p in team.picks):
            raise ProviderDataError(
                "official FPL picks contain unknown players", provider_id=PROVIDER_ID
            )
        acquired = datetime.now(UTC)
        picks = tuple(
            RawManagerPick(
                element_id=p.element,
                position=p.position,
                is_captain=p.is_captain,
                is_vice_captain=p.is_vice_captain,
            )
            for p in team.picks
        )
        starters = tuple(p.element for p in team.picks if p.position <= 11)
        bench = tuple(p.element for p in team.picks if p.position > 11)
        goalkeeper = next(
            (player for player in bench if types[player] is Position.GOALKEEPER),
            None,
        )
        if goalkeeper is None:
            raise ProviderDataError(
                "official FPL response has no reserve goalkeeper",
                provider_id=PROVIDER_ID,
            )
        outfield = tuple(player for player in bench if player != goalkeeper)
        return ManagerStateSnapshot(
            source_provider=PROVIDER_ID,
            source_endpoint=f"/api/my-team/{entry_id}/",
            acquired_at_utc=acquired,
            manager_entry_id=entry_id,
            authenticated_entry_id=me.player_id,
            target_event_id=target_event,
            target_deadline_time=event.deadline_time,
            raw_picks=picks,
            squad_player_ids=tuple(p.element for p in team.picks),
            starting_xi_player_ids=starters,
            captain_player_id=next(p.element for p in team.picks if p.is_captain),
            vice_captain_player_id=next(p.element for p in team.picks if p.is_vice_captain),
            reserve_goalkeeper_player_id=goalkeeper,
            ordered_outfield_bench_player_ids=outfield,
            bank=team.last_deadline_bank,
            team_value=team.last_deadline_value,
        )
