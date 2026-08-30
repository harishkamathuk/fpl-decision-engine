"""Offline adapter for final official FPL Gameweek live outcomes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from fpl_decision_engine.domain import ExternalRef, GameweekNumber, RealisedOutcome
from fpl_decision_engine.ports import ProviderDataError, ProviderMappingError


class OutcomeSnapshotNotFinalError(ProviderDataError):
    """The supplied official source has not reached the approved finality state."""


@dataclass(frozen=True, slots=True)
class FplOutcomeSources:
    """Frozen bytes and metadata for bootstrap, fixtures and one live endpoint."""

    season: str
    gameweek: int
    bootstrap: bytes
    fixtures: bytes
    live: bytes
    source_reference: str
    snapshot_id: str
    retrieved_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class FplOutcomeSnapshot:
    """Finality-validated realised outcomes and exact source provenance."""

    outcomes: Mapping[tuple[str, int, UUID], RealisedOutcome]
    provider_id: str
    provider_version: str
    season: str
    gameweek: int
    source_reference: str
    snapshot_id: str
    retrieved_at: AwareDatetime
    bootstrap_sha256: str
    fixtures_sha256: str
    live_sha256: str
    finality: tuple[tuple[str, str], ...]


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    finished: bool
    data_checked: bool


class _Fixture(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: int | None = None
    finished: bool


class _Stats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    starts: int
    minutes: int


class _LiveRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    element: int
    stats: _Stats


def _json(data: bytes, name: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderDataError(f"malformed JSON in {name}", provider_id="fpl") from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finality(sources: FplOutcomeSources) -> tuple[tuple[str, str], ...]:
    value = _json(sources.bootstrap, "bootstrap-static")
    if not isinstance(value, dict):
        raise ProviderDataError("bootstrap-static must be a JSON object", provider_id="fpl")
    raw = cast(dict[str, object], value)
    events_value = raw.get("events")
    if not isinstance(events_value, list):
        raise ProviderDataError("bootstrap-static events must be a JSON array", provider_id="fpl")
    event_values: list[object] = cast(list[object], events_value)
    try:
        event_items: list[dict[str, object]] = []
        for event_item in event_values:
            if isinstance(event_item, dict):
                event_items.append(cast(dict[str, object], event_item))
        event = next(
            _Event.model_validate(item)
            for item in event_items
            if item.get("id") == sources.gameweek
        )
    except StopIteration as exc:
        raise ProviderDataError(
            "target Gameweek is missing from bootstrap-static", provider_id="fpl"
        ) from exc
    fixture_value = _json(sources.fixtures, "fixtures")
    if not isinstance(fixture_value, list):
        raise ProviderDataError("fixtures must be a JSON array", provider_id="fpl")
    fixture_items: list[dict[str, object]] = []
    fixture_values: list[object] = cast(list[object], fixture_value)
    for fixture_item in fixture_values:
        if isinstance(fixture_item, dict):
            fixture_items.append(cast(dict[str, object], fixture_item))
    fixtures = tuple(
        _Fixture.model_validate(item)
        for item in fixture_items
        if item.get("event") == sources.gameweek
    )
    if not fixtures:
        raise OutcomeSnapshotNotFinalError(
            "no fixtures found for target Gameweek", provider_id="fpl"
        )
    checks = (
        ("event_finished", event.finished),
        ("event_data_checked", event.data_checked),
        ("fixtures_finished", all(item.finished for item in fixtures)),
    )
    failed = tuple(name for name, passed in checks if not passed)
    if failed:
        raise OutcomeSnapshotNotFinalError(
            "official outcome snapshot is not final: " + ", ".join(failed), provider_id="fpl"
        )
    return tuple((name, "true") for name, _ in checks)


def parse_final_fpl_outcomes(
    sources: FplOutcomeSources,
    *,
    element_to_player: Mapping[int, UUID],
    provider_version: str = "official-fpl-api-v1",
) -> FplOutcomeSnapshot:
    """Validate finality, then parse exact live rows into canonical outcomes."""

    finality = _finality(sources)
    value = _json(sources.live, f"event/{sources.gameweek}/live")
    if not isinstance(value, dict):
        raise ProviderDataError("live response must be a JSON object", provider_id="fpl")
    raw = cast(dict[str, object], value)
    elements_value = raw.get("elements")
    if not isinstance(elements_value, list):
        raise ProviderDataError("live response elements must be a JSON array", provider_id="fpl")
    element_values: list[object] = cast(list[object], elements_value)
    try:
        live_items: list[dict[str, object]] = []
        for live_item in element_values:
            if not isinstance(live_item, dict):
                raise ValueError("live outcome row must be an object")
            live_items.append(cast(dict[str, object], live_item))
        rows = tuple(_LiveRow.model_validate(item) for item in live_items)
    except ValidationError as exc:
        raise ProviderDataError(f"invalid live outcome row: {exc}", provider_id="fpl") from exc
    outcomes: dict[tuple[str, int, UUID], RealisedOutcome] = {}
    seen_elements: set[int] = set()
    for row in rows:
        if row.element in seen_elements:
            raise ProviderDataError(
                f"duplicate live outcome for element {row.element}", provider_id="fpl"
            )
        seen_elements.add(row.element)
        if row.element not in element_to_player:
            raise ProviderMappingError(
                f"unmapped official FPL element ID: {row.element}", provider_id="fpl"
            )
        player_id = element_to_player[row.element]
        record = RealisedOutcome(
            season=sources.season,
            gameweek=GameweekNumber(value=sources.gameweek),
            player_ref=ExternalRef(provider="fpl-element", external_id=str(row.element)),
            canonical_player_id=player_id,
            started=row.stats.starts > 0,
            minutes=row.stats.minutes,
            source_reference=f"{sources.source_reference}/api/event/{sources.gameweek}/live/",
            provider_id="fpl",
            provider_version=provider_version,
            snapshot_id=sources.snapshot_id,
            retrieved_at=sources.retrieved_at,
            finalised_at=sources.retrieved_at,
        )
        outcomes[record.logical_identity] = record
    return FplOutcomeSnapshot(
        outcomes=outcomes,
        provider_id="fpl",
        provider_version=provider_version,
        season=sources.season,
        gameweek=sources.gameweek,
        source_reference=sources.source_reference,
        snapshot_id=sources.snapshot_id,
        retrieved_at=sources.retrieved_at,
        bootstrap_sha256=_digest(sources.bootstrap),
        fixtures_sha256=_digest(sources.fixtures),
        live_sha256=_digest(sources.live),
        finality=finality,
    )
