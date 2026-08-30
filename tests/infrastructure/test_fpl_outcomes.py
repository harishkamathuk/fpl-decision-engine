"""Acceptance tests for the offline official FPL outcome adapter."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from fpl_decision_engine.infrastructure.providers.fpl_snapshot import (
    FplOutcomeSources,
    OutcomeSnapshotNotFinalError,
    parse_final_fpl_outcomes,
)
from fpl_decision_engine.ports import ProviderDataError, ProviderMappingError

NOW = datetime(2026, 8, 30, 15, tzinfo=UTC)


def sources(*, finished=True, data_checked=True, fixture_finished=True, live=None):
    bootstrap = {
        "events": [{"id": 1, "finished": finished, "data_checked": data_checked}]
    }
    fixtures = [{"id": 1, "event": 1, "finished": fixture_finished}]
    payload = live if live is not None else {
        "elements": [{"element": 101, "stats": {"starts": 0, "minutes": 0}}]
    }
    return FplOutcomeSources(
        season="2026-27", gameweek=1, bootstrap=json.dumps(bootstrap).encode(),
        fixtures=json.dumps(fixtures).encode(), live=json.dumps(payload).encode(),
        source_reference="fixture://fpl", snapshot_id="snapshot-1", retrieved_at=NOW,
    )


def test_finality_accepts_complete_sources_and_rejects_each_failed_condition() -> None:
    accepted = parse_final_fpl_outcomes(sources(), element_to_player={101: UUID(int=1)})
    assert accepted.finality == (
        ("event_finished", "true"),
        ("event_data_checked", "true"),
        ("fixtures_finished", "true"),
    )
    for kwargs in (
        {"finished": False}, {"data_checked": False}, {"fixture_finished": False}
    ):
        with pytest.raises(OutcomeSnapshotNotFinalError):
            parse_final_fpl_outcomes(sources(**kwargs), element_to_player={101: UUID(int=1)})


def test_live_starts_minutes_identity_and_replay_are_exact() -> None:
    payload = {"elements": [
        {"element": 101, "stats": {"starts": 1, "minutes": 90}},
        {"element": 102, "stats": {"starts": 0, "minutes": 0}},
    ]}
    source = sources(live=payload)
    mapping = {101: UUID(int=1), 102: UUID(int=2)}
    first = parse_final_fpl_outcomes(source, element_to_player=mapping)
    second = parse_final_fpl_outcomes(source, element_to_player=mapping)
    assert first == second
    assert first.outcomes[("2026-27", 1, UUID(int=1))].started is True
    assert first.outcomes[("2026-27", 1, UUID(int=1))].minutes == 90
    assert first.outcomes[("2026-27", 1, UUID(int=2))].started is False
    assert first.live_sha256 == hashlib.sha256(source.live).hexdigest()


def test_missing_fields_malformed_stats_and_unmapped_identity_fail() -> None:
    for row in (
        {"element": 101, "stats": {"minutes": 1}},
        {"element": 101, "stats": {"starts": 1}},
        {"element": 101, "stats": None},
    ):
        with pytest.raises(ProviderDataError):
            parse_final_fpl_outcomes(
                sources(live={"elements": [row]}), element_to_player={101: UUID(int=1)}
            )
    with pytest.raises(ProviderMappingError, match="unmapped"):
        parse_final_fpl_outcomes(sources(), element_to_player={})
    changed = sources(
        live={"elements": [{"element": 101, "stats": {"starts": 1, "minutes": 1}}]}
    )
    assert changed.live != sources().live
