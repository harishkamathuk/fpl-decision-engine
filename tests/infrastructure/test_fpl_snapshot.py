from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.infrastructure.ingestion import SnapshotStore, prepare_snapshot
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import map_snapshot
from fpl_decision_engine.ports import (
    ProviderDataError,
    ProviderErrorCode,
    ProviderMappingError,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "fpl_snapshot"
IMPORTED_AT = datetime(2026, 8, 14, 12, 5, tzinfo=UTC)


def fixture_copy(tmp_path: Path) -> Path:
    target = tmp_path / "input"
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def update_json(path: Path, update: object) -> None:
    path.write_text(json.dumps(update, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_valid_snapshot_maps_deterministically_and_preserves_source_values(
    tmp_path: Path,
) -> None:
    source = fixture_copy(tmp_path)
    first = map_snapshot(prepare_snapshot(source))
    second = map_snapshot(prepare_snapshot(source))

    assert first == second
    assert first.season == "2026-27"
    assert first.gameweeks[0].deadline_at.tzinfo is not None
    assert first.players[1].price.tenths_million == 75
    assert first.players[1].external_refs[0].provider == "synthetic-fpl"
    assert first.players[1].external_refs[0].external_id == "202"
    assert isinstance(first.players[1].id, UUID)
    assert len(first.warnings) == 1


def test_snapshot_store_persists_exact_bytes_and_idempotently_reuses_snapshot(
    tmp_path: Path,
) -> None:
    source = fixture_copy(tmp_path)
    prepared = prepare_snapshot(source)
    canonical = map_snapshot(prepared)
    prepared = prepared.with_season(canonical.season)
    store = SnapshotStore(tmp_path / "raw")

    first = store.store(prepared, imported_at=IMPORTED_AT)
    second = store.store(prepared, imported_at=IMPORTED_AT.replace(minute=10))

    assert first.created
    assert not second.created
    assert first.path == second.path
    assert second.manifest.imported_at == IMPORTED_AT
    assert (first.path / "bootstrap-static.json").read_bytes() == (
        source / "bootstrap-static.json"
    ).read_bytes()
    assert (first.path / "fixtures.json").read_bytes() == (source / "fixtures.json").read_bytes()
    assert first.manifest.snapshot_id.endswith(prepared.content_hash[:12])
    assert {item.resource_name for item in first.manifest.source_objects} == {
        "bootstrap-static",
        "fixtures",
    }


def test_changed_content_cannot_overwrite_existing_snapshot_identity(tmp_path: Path) -> None:
    source = fixture_copy(tmp_path)
    prepared = prepare_snapshot(source)
    canonical = map_snapshot(prepared)
    prepared = prepared.with_season(canonical.season)
    store = SnapshotStore(tmp_path / "raw")
    first = store.store(prepared, imported_at=IMPORTED_AT)

    manifest = load_json(source / "manifest.json")
    assert isinstance(manifest, dict)
    manifest["snapshot_id"] = first.manifest.snapshot_id
    update_json(source / "manifest.json", manifest)
    bootstrap = load_json(source / "bootstrap-static.json")
    assert isinstance(bootstrap, dict)
    elements = bootstrap["elements"]
    assert isinstance(elements, list)
    elements[0]["web_name"] = "Changed"
    update_json(source / "bootstrap-static.json", bootstrap)
    changed = prepare_snapshot(source).with_season(canonical.season)

    with pytest.raises(ProviderDataError, match="immutable snapshot conflict") as error:
        store.store(changed, imported_at=IMPORTED_AT)
    assert error.value.code is ProviderErrorCode.INVALID_DATA


def test_missing_required_source_file_fails(tmp_path: Path) -> None:
    source = fixture_copy(tmp_path)
    (source / "fixtures.json").unlink()

    with pytest.raises(ProviderDataError, match="missing required source file"):
        prepare_snapshot(source)


def test_malformed_json_has_machine_readable_provider_error(tmp_path: Path) -> None:
    source = fixture_copy(tmp_path)
    (source / "fixtures.json").write_text("[{", encoding="utf-8")

    with pytest.raises(ProviderDataError, match="malformed JSON") as error:
        map_snapshot(prepare_snapshot(source))
    assert error.value.code is ProviderErrorCode.INVALID_DATA
    assert error.value.provider_id == "synthetic-fpl"


def test_player_referencing_unknown_team_is_mapping_failure(tmp_path: Path) -> None:
    source = fixture_copy(tmp_path)
    bootstrap = load_json(source / "bootstrap-static.json")
    assert isinstance(bootstrap, dict)
    elements = bootstrap["elements"]
    assert isinstance(elements, list)
    elements[0]["team"] = 999
    update_json(source / "bootstrap-static.json", bootstrap)

    with pytest.raises(ProviderMappingError, match="references unknown team") as error:
        map_snapshot(prepare_snapshot(source))
    assert error.value.code is ProviderErrorCode.MAPPING


def test_same_home_and_away_team_is_domain_mapping_failure(tmp_path: Path) -> None:
    source = fixture_copy(tmp_path)
    fixtures = load_json(source / "fixtures.json")
    assert isinstance(fixtures, list)
    fixtures[0]["team_a"] = fixtures[0]["team_h"]
    update_json(source / "fixtures.json", fixtures)

    with pytest.raises(ProviderMappingError, match="home and away teams must differ") as error:
        map_snapshot(prepare_snapshot(source))
    assert error.value.code is ProviderErrorCode.MAPPING


def test_manifest_season_guard_rejects_mismatched_source_deadline(tmp_path: Path) -> None:
    source = fixture_copy(tmp_path)
    manifest = load_json(source / "manifest.json")
    assert isinstance(manifest, dict)
    manifest["season"] = "2025-26"
    update_json(source / "manifest.json", manifest)

    with pytest.raises(ProviderDataError, match="does not match source season"):
        map_snapshot(prepare_snapshot(source))
