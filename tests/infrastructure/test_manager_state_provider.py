from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    ExternalRef,
    GameweekNumber,
    Money,
    Player,
    Position,
)
from fpl_decision_engine.infrastructure.providers.manager_state import (
    LocalFplManagerStateProvider,
)
from fpl_decision_engine.ports import (
    ManagerStateProvider,
    ProviderCapability,
    ProviderDataError,
    ProviderMappingError,
)

OBSERVED_AT = datetime(2026, 8, 15, 12, tzinfo=UTC)


def canonical_players() -> tuple[Player, ...]:
    positions = (
        [Position.GOALKEEPER] * 2
        + [Position.DEFENDER] * 5
        + [Position.MIDFIELDER] * 5
        + [Position.FORWARD] * 3
    )
    return tuple(
        Player(
            id=UUID(int=index),
            team_id=UUID(int=100 + ((index - 1) // 3)),
            first_name=f"First{index}",
            last_name=f"Last{index}",
            web_name=f"P{index}",
            position=position,
            price=Money(tenths_million=49 + index),
            external_refs=(ExternalRef(provider="fpl", external_id=str(1000 + index)),),
        )
        for index, position in enumerate(positions, start=1)
    )


def write_payload(
    path: Path, *, element_override: int | None = None, chip: str | None = None
) -> None:
    players = canonical_players()
    picks = [
        {
            "element": 1000 + index,
            "purchase_price": player.price.tenths_million,
            "selling_price": player.price.tenths_million,
        }
        for index, player in enumerate(players, start=1)
    ]
    if element_override is not None:
        picks[0]["element"] = element_override
    path.write_text(
        json.dumps(
            {
                "manager_id": 4242,
                "gameweek": 1,
                "picks": picks,
                "transfers": {
                    "bank": 7,
                    "limit": 3,
                    "made": 1,
                    "cost": 4,
                    "active_chip": chip,
                },
            }
        ),
        encoding="utf-8",
    )


def test_local_manager_state_maps_exact_identity_prices_and_remaining_allowance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manager.json"
    write_payload(path)
    players = canonical_players()
    provider = LocalFplManagerStateProvider(
        path,
        players,
        identity_namespace="fpl",
        season="2026-27",
        observed_at=OBSERVED_AT,
    )

    response = provider.manager_state("4242", GameweekNumber(value=1))
    state = response.data

    assert isinstance(provider, ManagerStateProvider)
    assert provider.descriptor.supports(ProviderCapability.MANAGER_STATE)
    assert state.squad.members[0].player_id == players[0].id
    assert state.squad.members[0].purchase_price == players[0].price
    assert state.squad.members[0].selling_price == players[0].price
    assert state.bank == Money(tenths_million=7)
    assert state.free_transfers == 2
    assert state.transfers_made == 1
    assert state.existing_points_cost == 4
    assert response.provenance.source_sha256 is not None
    assert response.provenance.mapping_fingerprint is not None
    assert response.provenance.snapshot_id == f"sha256:{response.provenance.source_sha256}"
    assert response.freshness.as_of == OBSERVED_AT


def test_local_manager_state_preserves_valid_source_selling_price(tmp_path: Path) -> None:
    path = tmp_path / "manager.json"
    write_payload(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["picks"][0]["purchase_price"] = 48
    payload["picks"][0]["selling_price"] = 49
    path.write_text(json.dumps(payload), encoding="utf-8")

    provider = LocalFplManagerStateProvider(
        path,
        canonical_players(),
        identity_namespace="fpl",
        season="2026-27",
        observed_at=OBSERVED_AT,
    )
    member = provider.manager_state("4242", GameweekNumber(value=1)).data.squad.members[0]

    assert canonical_players()[0].price == Money(tenths_million=50)
    assert member.purchase_price == Money(tenths_million=48)
    assert member.selling_price == Money(tenths_million=49)


@pytest.mark.parametrize("missing_field", ["purchase_price", "selling_price"])
def test_local_manager_state_rejects_missing_required_price(
    tmp_path: Path, missing_field: str
) -> None:
    path = tmp_path / "manager.json"
    write_payload(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["picks"][0][missing_field]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProviderDataError, match="invalid FPL-shaped manager state"):
        LocalFplManagerStateProvider(
            path,
            canonical_players(),
            identity_namespace="fpl",
            season="2026-27",
            observed_at=OBSERVED_AT,
        )


def test_local_manager_state_rejects_unknown_exact_player_identity(tmp_path: Path) -> None:
    path = tmp_path / "manager.json"
    write_payload(path, element_override=999999)

    with pytest.raises(ProviderMappingError, match="not mapped"):
        LocalFplManagerStateProvider(
            path,
            canonical_players(),
            identity_namespace="fpl",
            season="2026-27",
            observed_at=OBSERVED_AT,
        )


def test_local_manager_state_rejects_chip_active_state(tmp_path: Path) -> None:
    path = tmp_path / "manager.json"
    write_payload(path, chip="wildcard")

    with pytest.raises(ProviderDataError, match="chip-active"):
        LocalFplManagerStateProvider(
            path,
            canonical_players(),
            identity_namespace="fpl",
            season="2026-27",
            observed_at=OBSERVED_AT,
        )


def test_local_manager_state_rejects_inconsistent_selling_price(tmp_path: Path) -> None:
    path = tmp_path / "manager.json"
    write_payload(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["picks"][0]["selling_price"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProviderDataError, match="official rule"):
        LocalFplManagerStateProvider(
            path,
            canonical_players(),
            identity_namespace="fpl",
            season="2026-27",
            observed_at=OBSERVED_AT,
        )
