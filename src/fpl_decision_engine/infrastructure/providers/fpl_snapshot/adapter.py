"""Map validated FPL-shaped source records into the canonical domain."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import TypeAdapter, ValidationError

from fpl_decision_engine.domain import (
    ExternalRef,
    Fixture,
    Gameweek,
    GameweekNumber,
    Money,
    Player,
    Position,
    Team,
)
from fpl_decision_engine.infrastructure.ingestion import PreparedSnapshot
from fpl_decision_engine.ports import ProviderDataError, ProviderMappingError

from .schemas import SourceBootstrap, SourceFixture

IDENTITY_NAMESPACE = UUID("3d50b622-ccb5-5cc8-a3f5-7a61a263649b")
FPL_CODE_PROVIDER_ID = "fpl_code"
POSITION_BY_ELEMENT_TYPE = {
    1: Position.GOALKEEPER,
    2: Position.DEFENDER,
    3: Position.MIDFIELDER,
    4: Position.FORWARD,
}
_FIXTURES_ADAPTER = TypeAdapter(tuple[SourceFixture, ...])


@dataclass(frozen=True, slots=True)
class CanonicalFplSnapshot:
    season: str
    gameweeks: tuple[Gameweek, ...]
    teams: tuple[Team, ...]
    players: tuple[Player, ...]
    fixtures: tuple[Fixture, ...]
    warnings: tuple[str, ...]


def _provider_data_error(message: str, provider_id: str, exc: Exception) -> ProviderDataError:
    return ProviderDataError(message, provider_id=provider_id)


def _load_json(data: bytes, resource_name: str, provider_id: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _provider_data_error(
            f"malformed JSON in {resource_name}",
            provider_id,
            exc,
        ) from exc


def _source_uuid(provider_id: str, season: str, entity_type: str, external_id: int) -> UUID:
    identity_key = f"{provider_id}:{season}:{entity_type}:{external_id}"
    return uuid5(IDENTITY_NAMESPACE, identity_key)


def _player_external_refs(
    provider_id: str, external_id: int, code: int | None
) -> tuple[ExternalRef, ...]:
    """Keep season-specific element IDs separate from stable cross-season FPL codes."""

    refs = [ExternalRef(provider=provider_id, external_id=str(external_id))]
    if code is not None:
        refs.append(ExternalRef(provider=FPL_CODE_PROVIDER_ID, external_id=str(code)))
    return tuple(refs)


def _derive_season(bootstrap: SourceBootstrap, provider_id: str) -> str:
    if not bootstrap.events:
        raise ProviderDataError("bootstrap events collection is empty", provider_id=provider_id)
    first_deadline = min(event.deadline_time for event in bootstrap.events)
    start_year = first_deadline.year
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def map_snapshot(snapshot: PreparedSnapshot) -> CanonicalFplSnapshot:
    """Validate a complete source snapshot and map it without partial results."""

    provider_id = snapshot.provider_id
    bootstrap_value = _load_json(
        snapshot.object_bytes("bootstrap-static"),
        "bootstrap-static",
        provider_id,
    )
    fixtures_value = _load_json(
        snapshot.object_bytes("fixtures"),
        "fixtures",
        provider_id,
    )
    try:
        bootstrap = SourceBootstrap.model_validate(bootstrap_value)
        source_fixtures = _FIXTURES_ADAPTER.validate_python(fixtures_value)
    except ValidationError as exc:
        raise ProviderDataError(
            f"unsupported or incomplete FPL-shaped source data: {exc}",
            provider_id=provider_id,
        ) from exc

    season = _derive_season(bootstrap, provider_id)
    if snapshot.season is not None and snapshot.season != season:
        raise ProviderDataError(
            f"snapshot season {snapshot.season!r} does not match source season {season!r}",
            provider_id=provider_id,
        )

    team_ids: dict[int, UUID] = {}
    teams: list[Team] = []
    try:
        for source_team in bootstrap.teams:
            if source_team.id in team_ids:
                raise ValueError(f"duplicate team external ID: {source_team.id}")
            team_id = _source_uuid(provider_id, season, "team", source_team.id)
            team_ids[source_team.id] = team_id
            teams.append(
                Team(
                    id=team_id,
                    name=source_team.name,
                    short_name=source_team.short_name,
                    external_refs=(
                        ExternalRef(provider=provider_id, external_id=str(source_team.id)),
                    ),
                )
            )

        gameweeks = tuple(
            Gameweek(
                number=GameweekNumber(value=event.id),
                name=event.name,
                deadline_at=event.deadline_time,
                finished=event.finished,
            )
            for event in bootstrap.events
        )
        known_gameweeks = {gameweek.number.value for gameweek in gameweeks}

        players: list[Player] = []
        player_external_ids: set[int] = set()
        warnings: list[str] = []
        for source_player in bootstrap.elements:
            if source_player.id in player_external_ids:
                raise ValueError(f"duplicate player external ID: {source_player.id}")
            player_external_ids.add(source_player.id)
            try:
                team_id = team_ids[source_player.team]
            except KeyError as exc:
                raise ProviderMappingError(
                    f"player {source_player.id} references unknown team {source_player.team}",
                    provider_id=provider_id,
                ) from exc
            try:
                position = POSITION_BY_ELEMENT_TYPE[source_player.element_type]
            except KeyError as exc:
                raise ProviderMappingError(
                    f"player {source_player.id} has unsupported element_type "
                    f"{source_player.element_type}",
                    provider_id=provider_id,
                ) from exc
            if source_player.status != "a" or source_player.news:
                warnings.append(
                    f"player {source_player.id} has source availability status "
                    f"{source_player.status!r}"
                )
            players.append(
                Player(
                    id=_source_uuid(provider_id, season, "player", source_player.id),
                    team_id=team_id,
                    first_name=source_player.first_name,
                    last_name=source_player.second_name,
                    web_name=source_player.web_name,
                    position=position,
                    price=Money(tenths_million=source_player.now_cost),
                    active=source_player.status == "a",
                    external_refs=_player_external_refs(
                        provider_id, source_player.id, source_player.code
                    ),
                )
            )

        fixtures: list[Fixture] = []
        fixture_external_ids: set[int] = set()
        for source_fixture in source_fixtures:
            if source_fixture.id in fixture_external_ids:
                raise ValueError(f"duplicate fixture external ID: {source_fixture.id}")
            fixture_external_ids.add(source_fixture.id)
            if source_fixture.event is not None and source_fixture.event not in known_gameweeks:
                raise ProviderMappingError(
                    f"fixture {source_fixture.id} references unknown gameweek "
                    f"{source_fixture.event}",
                    provider_id=provider_id,
                )
            try:
                home_team_id = team_ids[source_fixture.team_h]
                away_team_id = team_ids[source_fixture.team_a]
            except KeyError as exc:
                raise ProviderMappingError(
                    f"fixture {source_fixture.id} references unknown team {exc.args[0]}",
                    provider_id=provider_id,
                ) from exc
            fixtures.append(
                Fixture(
                    id=_source_uuid(provider_id, season, "fixture", source_fixture.id),
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    kickoff_at=source_fixture.kickoff_time,
                    gameweek=(
                        GameweekNumber(value=source_fixture.event)
                        if source_fixture.event is not None
                        else None
                    ),
                    external_refs=(
                        ExternalRef(provider=provider_id, external_id=str(source_fixture.id)),
                    ),
                )
            )
    except ProviderMappingError:
        raise
    except (ValidationError, ValueError) as exc:
        raise ProviderMappingError(
            f"source data cannot be mapped to the canonical domain: {exc}",
            provider_id=provider_id,
        ) from exc

    return CanonicalFplSnapshot(
        season=season,
        gameweeks=gameweeks,
        teams=tuple(teams),
        players=tuple(players),
        fixtures=tuple(fixtures),
        warnings=tuple(warnings),
    )
