"""Infrastructure-level tests for StructuredTeamNewsEvidenceProvider."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    Money,
    Player,
)
from fpl_decision_engine.infrastructure.providers.team_news.structured import (
    StructuredTeamNewsEvidenceProvider,
)
from fpl_decision_engine.ports import (
    ProviderCapability,
    ProviderDataError,
    ProviderMappingError,
)

PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "fixtures"
    / "team_news"
    / "structured_evidence_v1.json"
)


def _players_with_team_news_refs():
    players = [
        Player(
            id=UUID("11111111-1111-1111-1111-111111111111"),
            team_id=UUID("22222222-2222-2222-2222-222222222222"),
            first_name="Alice",
            last_name="Smith",
            web_name="ASmith",
            position="MID",
            price=Money(tenths_million=100),
            active=True,
            external_refs=(
                {"provider": "team_news_api", "external_id": "10001"},
                {"provider": "fpl", "external_id": "1"},
            ),
        ),
        Player(
            id=UUID("22222222-2222-2222-2222-222222222222"),
            team_id=UUID("33333333-3333-3333-3333-333333333333"),
            first_name="Bob",
            last_name="Jones",
            web_name="BJones",
            position="FWD",
            price=Money(tenths_million=105),
            active=True,
            external_refs=(
                {"provider": "team_news_api", "external_id": "10002"},
                {"provider": "fpl", "external_id": "2"},
            ),
        ),
        Player(
            id=UUID("33333333-3333-3333-3333-333333333333"),
            team_id=UUID("44444444-4444-4444-4444-444444444444"),
            first_name="Charlie",
            last_name="Brown",
            web_name="CBrown",
            position="DEF",
            price=Money(tenths_million=95),
            active=True,
            external_refs=(
                {"provider": "team_news_api", "external_id": "10003"},
                {"provider": "fpl", "external_id": "3"},
            ),
        ),
    ]
    return players


def test_valid_structured_artefact_maps_to_canonical_availability_evidence() -> None:
    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    item = provider.evidence().data[0]
    assert item.evidence_id == "tn-001"
    assert item.player_id == UUID("11111111-1111-1111-1111-111111111111")


def test_provider_satisfies_news_evidence_provider() -> None:
    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    assert provider.descriptor.capabilities == frozenset({ProviderCapability.NEWS_EVIDENCE})


def test_distinct_timestamps() -> None:
    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    items = provider.evidence().data
    assert items[0].observed_at != items[0].processed_at


def test_source_reference_and_attributes_retained() -> None:
    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    items = provider.evidence().data
    assert items[0].attributes[0].name == "source_reference"
    assert items[0].attributes[0].value == "Injury update: Hamstring strain"
    assert items[0].attributes[1].name == "predicted_xi"
    assert items[0].attributes[1].value == "false"
    assert items[0].attributes[2].name == "training_status"
    assert items[0].attributes[2].value == "limited"


def test_raw_sha256() -> None:
    import hashlib

    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    expected_sha256 = hashlib.sha256(PATH.read_bytes()).hexdigest()
    assert provider.evidence().provenance.source_sha256 == expected_sha256


def test_deterministic_mapping_fingerprint_same_fingerprint() -> None:
    provider1 = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    provider2 = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    assert provider1._mapping_fingerprint == provider2._mapping_fingerprint


def test_missing_published_at_remains_none() -> None:
    data = {
        "schema_version": 1,
        "source_provider": "team_news_api",
        "source_snapshot_id": "20260814T200000Z_abc123def456",
        "observed_at": "2026-08-14T20:15:30Z",
        "evidence": [
            {
                "evidence_id": "tn-no-pub",
                "source_external_player_id": "10001",
                "source_reference": "No published date",
                "state": "available",
                "reason": "available",
                "confidence": "indicative",
                "source_text": None,
                "attributes": None,
            }
        ],
    }
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    provider = StructuredTeamNewsEvidenceProvider(
        tmp,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    item = provider.evidence().data[0]
    assert item.published_at is None


def test_unmapped_external_identity_raises_provider_mapping_error() -> None:
    from uuid import UUID

    from fpl_decision_engine.domain import Player

    p = Player(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        team_id=UUID("55555555-5555-5555-5555-555555555555"),
        first_name="Dave",
        last_name="Smith",
        web_name="DSmith",
        position="MID",
        price=Money(tenths_million=90),
        active=True,
        external_refs=({"provider": "fpl", "external_id": "4"},),
    )

    with pytest.raises(ProviderMappingError):
        StructuredTeamNewsEvidenceProvider(
            PATH, [p], processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_ambiguous_external_identity_raises_provider_mapping_error() -> None:
    p1 = Player(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        team_id=UUID("22222222-2222-2222-2222-222222222222"),
        first_name="Alice",
        last_name="Smith",
        web_name="ASmith",
        position="MID",
        price=Money(tenths_million=100),
        active=True,
        external_refs=({"provider": "team_news_api", "external_id": "10001"},),
    )
    p2 = Player(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        team_id=UUID("33333333-3333-3333-3333-333333333333"),
        first_name="Bob",
        last_name="Jones",
        web_name="BJones",
        position="FWD",
        price=Money(tenths_million=105),
        active=True,
        external_refs=({"provider": "team_news_api", "external_id": "10001"},),
    )

    with pytest.raises(ProviderMappingError):
        StructuredTeamNewsEvidenceProvider(
            PATH, [p1, p2], processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_multiple_external_refs_same_player_raises_provider_mapping_error() -> None:
    """Multiple ExternalRefs for same player provider raises ProviderMappingError."""
    p = Player(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        team_id=UUID("22222222-2222-2222-2222-222222222222"),
        first_name="Alice",
        last_name="Smith",
        web_name="ASmith",
        position="MID",
        price=Money(tenths_million=100),
        active=True,
        external_refs=(
            {"provider": "team_news_api", "external_id": "10001"},
            {"provider": "team_news_api", "external_id": "99999"},
        ),
    )

    with pytest.raises(ProviderMappingError):
        StructuredTeamNewsEvidenceProvider(
            PATH, [p], processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_malformed_json_fails() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{ invalid json }")
        tmp = Path(f.name)

    players = _players_with_team_news_refs()
    with pytest.raises(ProviderDataError):
        StructuredTeamNewsEvidenceProvider(
            tmp, players, processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_unsupported_schema_version_fails() -> None:
    import tempfile
    from pathlib import Path

    data = {
        "schema_version": 2,
        "source_provider": "team_news_api",
        "source_snapshot_id": "20260814T200000Z_abc123def456",
        "observed_at": "2026-08-14T20:15:30Z",
        "evidence": [],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)

    players = _players_with_team_news_refs()
    with pytest.raises(ProviderDataError):
        StructuredTeamNewsEvidenceProvider(
            tmp, players, processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_naive_timestamps_fail() -> None:
    import tempfile
    from pathlib import Path

    data = {
        "schema_version": 1,
        "source_provider": "team_news_api",
        "source_snapshot_id": "2026-08-14T20:15:30Z",
        "evidence": [
            {
                "evidence_id": "tn-naive",
                "source_external_player_id": "10001",
                "source_reference": "Naive timestamp",
                "state": "available",
                "reason": "available",
                "confidence": "indicative",
                "published_at": "2026-08-14T19:30:00",
                "source_text": "Test",
                "attributes": None,
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)

    players = _players_with_team_news_refs()
    with pytest.raises(ProviderDataError):
        StructuredTeamNewsEvidenceProvider(
            tmp, players, processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_processed_at_before_observed_at_fails() -> None:
    """Invalid processing chronology is exposed as a provider data error."""
    processed_at = datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC)

    with pytest.raises(ProviderDataError) as exc_info:
        StructuredTeamNewsEvidenceProvider(
            PATH,
            _players_with_team_news_refs(),
            processed_at=processed_at,
        )

    assert str(exc_info.value) == "processed_at cannot precede artifact observed_at"


def test_published_at_greater_than_observed_at_fails() -> None:
    """Invalid chronology (published_at > observed_at) is rejected as ProviderDataError."""
    import tempfile
    from pathlib import Path

    data = {
        "schema_version": 1,
        "source_provider": "team_news_api",
        "source_snapshot_id": "20260814T200000Z_abc123def456",
        "observed_at": "2026-08-14T20:15:30Z",
        "evidence": [
            {
                "evidence_id": "tn-conflict",
                "source_external_player_id": "10001",
                "source_reference": "Conflict evidence",
                "state": "available",
                "reason": "available",
                "confidence": "indicative",
                "published_at": "2026-08-14T21:00:00Z",
                "source_text": "Test",
                "attributes": None,
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)

    players = _players_with_team_news_refs()

    with pytest.raises(ProviderDataError) as exc_info:
        provider = StructuredTeamNewsEvidenceProvider(
            tmp, players, processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )
        provider.evidence()

    # Provider rejects published_at > observed_at chronology as ProviderDataError
    assert "availability evidence cannot be observed before publication" in str(exc_info.value)

    # Provider rejects published_at > observed_at chronology
    assert "availability evidence cannot be observed before publication" in str(exc_info.value)


def test_duplicate_evidence_ids_fail() -> None:
    import tempfile
    from pathlib import Path

    data = {
        "schema_version": 1,
        "source_provider": "team_news_api",
        "source_snapshot_id": "20260814T200000Z_abc123def456",
        "observed_at": "2026-08-14T20:15:30Z",
        "evidence": [
            {
                "evidence_id": "dup-1",
                "source_external_player_id": "10001",
                "source_reference": "Duplicate ID test 1",
                "state": "available",
                "reason": "available",
                "confidence": "indicative",
                "published_at": "2026-08-14T19:30:00Z",
                "source_text": "Test 1",
                "attributes": None,
            },
            {
                "evidence_id": "dup-1",
                "source_external_player_id": "10002",
                "source_reference": "Duplicate ID test 2",
                "state": "available",
                "reason": "available",
                "confidence": "indicative",
                "published_at": "2026-08-14T19:30:00Z",
                "source_text": "Test 2",
                "attributes": None,
            },
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)

    players = _players_with_team_news_refs()
    with pytest.raises(ProviderDataError):
        StructuredTeamNewsEvidenceProvider(
            tmp, players, processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_invalid_enum_values_fail() -> None:
    import tempfile
    from pathlib import Path

    data = {
        "schema_version": 1,
        "source_provider": "team_news_api",
        "source_snapshot_id": "20260814T200000Z_abc123def456",
        "observed_at": "2026-08-14T20:15:30Z",
        "evidence": [
            {
                "evidence_id": "tn-invalid-enum",
                "source_external_player_id": "10001",
                "source_reference": "Invalid enum test",
                "state": "invalid_state",
                "reason": "invalid_reason",
                "confidence": "invalid_confidence",
                "published_at": "2026-08-14T19:30:00Z",
                "source_text": "Test",
                "attributes": None,
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)

    players = _players_with_team_news_refs()
    with pytest.raises(ProviderDataError):
        StructuredTeamNewsEvidenceProvider(
            tmp, players, processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
        )  # noqa: E501


def test_requested_player_filtering_deterministic() -> None:
    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    response1 = provider.evidence(player_ids=[UUID("11111111-1111-1111-1111-111111111111")])
    response2 = provider.evidence(player_ids=[UUID("11111111-1111-1111-1111-111111111111")])
    assert len(response1.data) == 1
    assert len(response2.data) == 1
    assert response1.data[0].player_id == response2.data[0].player_id


def test_fpl_ref_ignored_for_team_news_provider() -> None:
    provider = StructuredTeamNewsEvidenceProvider(
        PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
    )  # noqa: E501
    items = provider.evidence().data
    assert items[0].source_external_player_id == "10001"


def test_fixture_file_exists() -> None:
    assert Path(PATH).exists()
