"""Deterministic tests for the source-specific Premier League injury collector."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from fpl_decision_engine.domain import ExternalRef, Money, Player
from fpl_decision_engine.infrastructure.providers.team_news import (
    PremierLeagueInjuriesCollector,
    parse_injury_page,
)
from fpl_decision_engine.infrastructure.providers.team_news.structured import (
    StructuredTeamNewsEvidenceProvider,
)
from fpl_decision_engine.ports import ProviderDataError, ProviderMappingError

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "team_news"
HTML_PATH = FIXTURE_ROOT / "premier_league_latest_injuries.html"
BOOTSTRAP_PATH = FIXTURE_ROOT / "bootstrap-static-team-news.json"
CAPTURED_AT = datetime(2026, 8, 19, 15, 0, tzinfo=UTC)


def _players() -> tuple[Player, ...]:
    return (
        Player(
            id=UUID("11111111-1111-1111-1111-111111111111"),
            team_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            first_name="First",
            last_name="One",
            web_name="One",
            position="MID",
            price=Money(tenths_million=100),
            external_refs=(ExternalRef(provider="fpl_code", external_id="101"),),
        ),
        Player(
            id=UUID("22222222-2222-2222-2222-222222222222"),
            team_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            first_name="Second",
            last_name="Player",
            web_name="Player",
            position="MID",
            price=Money(tenths_million=90),
            external_refs=(ExternalRef(provider="fpl_code", external_id="102"),),
        ),
        Player(
            id=UUID("33333333-3333-3333-3333-333333333333"),
            team_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            first_name="John",
            last_name="Smith",
            web_name="Smith",
            position="MID",
            price=Money(tenths_million=95),
            external_refs=(ExternalRef(provider="fpl_code", external_id="201"),),
        ),
    )


def _collector(
    tmp_path: Path,
    *,
    bootstrap_path: Path | None = BOOTSTRAP_PATH,
    player_overrides: dict[tuple[str, str], str] | None = None,
    canonical_players: tuple[Player, ...] = (),
) -> PremierLeagueInjuriesCollector:
    return PremierLeagueInjuriesCollector(
        tmp_path / "captures",
        bootstrap_path=bootstrap_path,
        canonical_players=canonical_players,
        player_overrides=player_overrides,
    )


def _html() -> bytes:
    return HTML_PATH.read_bytes()


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_valid_fixture_emits_57_artifact_and_evaluation(tmp_path: Path) -> None:
    result = _collector(tmp_path).collect_response(_html(), captured_at=CAPTURED_AT)

    assert result.structured_evidence_path is not None
    artifact = _load_json(result.structured_evidence_path)
    assert artifact["schema_version"] == 1
    assert artifact["source_provider"] == "fpl_code"
    assert artifact["source_snapshot_id"] == result.capture_id
    assert len(artifact["evidence"]) == 3  # type: ignore[arg-type]

    evaluation = _load_json(result.path / "evaluation.json")
    assert evaluation["collection_success"] is True
    assert evaluation["parse_success"] is True
    assert evaluation["source_rows_seen"] == 4  # includes Burnley's placeholder row
    assert evaluation["evidence_records_emitted"] == 3
    assert evaluation["exact_matches"] == 3
    assert evaluation["override_matches"] == 0
    rows = evaluation["rows"]
    assert isinstance(rows, list)
    assert rows[0]["classification"] == "PL injury row + FPL still status=a"
    assert rows[1]["classification"] == "PL injury row + FPL already flagged"


def test_raw_capture_hash_and_manifest_are_exact(tmp_path: Path) -> None:
    result = _collector(tmp_path).collect_response(_html(), captured_at=CAPTURED_AT)
    raw = result.path / "raw-response.bin"
    manifest = _load_json(result.path / "capture-manifest.json")

    assert raw.read_bytes() == _html()
    assert manifest["sha256"] == hashlib.sha256(_html()).hexdigest()
    assert manifest["byte_count"] == len(_html())
    assert manifest["logical_source_page"] == "https://www.premierleague.com/en/latest-player-injuries"
    assert manifest["http_status"] == 200
    assert manifest["content_type"] == "text/html;charset=utf-8"
    assert manifest["page_last_updated_text"] == "Last updated: 19 August 2026 at 14:30 BST"
    assert manifest["parser_version"] == "1"


def test_page_last_updated_uses_uk_bst_and_is_published_at(tmp_path: Path) -> None:
    parsed = parse_injury_page(_html())
    assert parsed.page_last_updated_at is not None
    assert parsed.page_last_updated_at.astimezone(UTC) == datetime(
        2026, 8, 19, 13, 30, tzinfo=UTC
    )

    result = _collector(tmp_path).collect_response(_html(), captured_at=CAPTURED_AT)
    artifact = _load_json(result.path / "structured-evidence.json")
    evidence = artifact["evidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["published_at"] == "2026-08-19T14:30:00+01:00"


def test_missing_publication_timestamp_remains_null(tmp_path: Path) -> None:
    source = _html().replace(
        b"Last updated: 19 August 2026 at 14:30 BST", b"Last updated"
    )
    result = _collector(tmp_path).collect_response(source, captured_at=CAPTURED_AT)
    artifact = _load_json(result.path / "structured-evidence.json")
    evidence = artifact["evidence"]
    assert isinstance(evidence, list)
    assert all(item["published_at"] is None for item in evidence)
    manifest = _load_json(result.path / "capture-manifest.json")
    assert manifest["page_last_updated_text"] == "Last updated"
    assert manifest["page_last_updated_at"] is None


def test_exact_team_scoped_mapping_emits_stable_fpl_codes(tmp_path: Path) -> None:
    result = _collector(tmp_path).collect_response(_html(), captured_at=CAPTURED_AT)
    artifact = _load_json(result.path / "structured-evidence.json")
    evidence = artifact["evidence"]
    assert isinstance(evidence, list)
    assert {item["source_external_player_id"] for item in evidence} == {"101", "102", "201"}


def test_reviewed_override_is_bootstrap_validated_and_used(tmp_path: Path) -> None:
    source = _html().replace(b"First One", b"First Alias")
    result = _collector(
        tmp_path,
        player_overrides={("Arsenal", "First Alias"): "101"},
    ).collect_response(source, captured_at=CAPTURED_AT)
    evaluation = _load_json(result.path / "evaluation.json")
    assert evaluation["override_matches"] == 1
    assert evaluation["exact_matches"] == 2


def test_invalid_override_fails_explicitly(tmp_path: Path) -> None:
    with pytest.raises(ProviderMappingError, match="invalid reviewed override"):
        _collector(
            tmp_path,
            player_overrides={("Arsenal", "First One"): "999999"},
        ).collect_response(_html(), captured_at=CAPTURED_AT)


def test_missing_stable_fpl_code_fails_with_no_mapping_classification(
    tmp_path: Path,
) -> None:
    bootstrap = _load_json(BOOTSTRAP_PATH)
    elements = bootstrap["elements"]
    assert isinstance(elements, list)
    elements[0]["code"] = None
    bootstrap_path = tmp_path / "bootstrap-missing-code.json"
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")

    with pytest.raises(ProviderMappingError, match="no stable FPL code"):
        _collector(tmp_path, bootstrap_path=bootstrap_path).collect_response(
            _html(), captured_at=CAPTURED_AT
        )

    capture = next((tmp_path / "captures" / "premier-league-injuries").iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert not (capture / "structured-evidence.json").exists()
    evaluation = _load_json(capture / "evaluation.json")
    assert evaluation["collection_success"] is False
    assert evaluation["unmapped_rows"] == 1
    rows = evaluation["rows"]
    assert isinstance(rows, list)
    assert rows[0]["mapping_status"] == "missing_fpl_code"
    assert rows[0]["classification"] == "PL injury row + no FPL mapping"


def test_non_unique_stable_fpl_code_fails_with_no_mapping_classification(
    tmp_path: Path,
) -> None:
    bootstrap = _load_json(BOOTSTRAP_PATH)
    elements = bootstrap["elements"]
    assert isinstance(elements, list)
    duplicate = dict(elements[0])
    duplicate["id"] = 99
    duplicate["team"] = 2
    duplicate["first_name"] = "Different"
    duplicate["second_name"] = "Name"
    elements.append(duplicate)
    bootstrap_path = tmp_path / "bootstrap-duplicate-code.json"
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")

    with pytest.raises(ProviderMappingError, match="is not unique"):
        _collector(tmp_path, bootstrap_path=bootstrap_path).collect_response(
            _html(), captured_at=CAPTURED_AT
        )

    capture = next((tmp_path / "captures" / "premier-league-injuries").iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert not (capture / "structured-evidence.json").exists()
    evaluation = _load_json(capture / "evaluation.json")
    assert evaluation["collection_success"] is False
    assert evaluation["unmapped_rows"] == 1
    rows = evaluation["rows"]
    assert isinstance(rows, list)
    assert rows[0]["mapping_status"] == "non_unique_fpl_code"
    assert rows[0]["classification"] == "PL injury row + no FPL mapping"


def test_unmapped_player_fails_and_preserves_raw_capture(tmp_path: Path) -> None:
    source = _html().replace(b"First One", b"No Such Player")
    with pytest.raises(ProviderMappingError, match="unmapped"):
        _collector(tmp_path).collect_response(source, captured_at=CAPTURED_AT)

    capture_root = tmp_path / "captures" / "premier-league-injuries"
    capture_paths = list(capture_root.iterdir())
    assert len(capture_paths) == 1
    capture = capture_paths[0]
    assert (capture / "raw-response.bin").read_bytes() == source
    assert (capture / "capture-manifest.json").is_file()
    evaluation = _load_json(capture / "evaluation.json")
    assert evaluation["collection_success"] is False
    assert evaluation["parse_success"] is True
    assert evaluation["unmapped_rows"] == 1
    rows = evaluation["rows"]
    assert isinstance(rows, list)
    assert rows[0]["mapping_status"] == "unmapped"


def test_ambiguous_exact_player_mapping_fails(tmp_path: Path) -> None:
    bootstrap = _load_json(BOOTSTRAP_PATH)
    elements = bootstrap["elements"]
    assert isinstance(elements, list)
    duplicate = dict(elements[0])
    duplicate["id"] = 99
    duplicate["code"] = 999
    elements.append(duplicate)
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")

    with pytest.raises(ProviderMappingError, match="ambiguous"):
        _collector(tmp_path, bootstrap_path=bootstrap_path).collect_response(
            _html(), captured_at=CAPTURED_AT
        )


def test_placeholder_club_row_emits_no_evidence(tmp_path: Path) -> None:
    result = _collector(tmp_path).collect_response(_html(), captured_at=CAPTURED_AT)
    artifact = _load_json(result.path / "structured-evidence.json")
    evidence = artifact["evidence"]
    assert isinstance(evidence, list)
    assert not any(item["attributes"]["club"] == "Burnley" for item in evidence)


def test_evidence_semantics_distinguish_explicit_and_blank_injury(tmp_path: Path) -> None:
    result = _collector(tmp_path).collect_response(_html(), captured_at=CAPTURED_AT)
    artifact = _load_json(result.path / "structured-evidence.json")
    evidence = artifact["evidence"]
    assert isinstance(evidence, list)
    by_code = {item["source_external_player_id"]: item for item in evidence}
    assert by_code["101"]["state"] == "doubtful"
    assert by_code["101"]["reason"] == "injury"
    assert by_code["101"]["confidence"] == "indicative"
    assert by_code["102"]["state"] == "unknown"
    assert by_code["102"]["reason"] == "injury"
    assert by_code["102"]["confidence"] == "ambiguous"


def test_changed_source_shape_fails_after_raw_capture(tmp_path: Path) -> None:
    source = _html().replace(b"<th>Latest</th>", b"<th>Detail</th>")
    with pytest.raises(ProviderDataError, match="expected an injury table"):
        _collector(tmp_path).collect_response(source, captured_at=CAPTURED_AT)

    capture_root = tmp_path / "captures" / "premier-league-injuries"
    capture = next(capture_root.iterdir())
    assert (capture / "raw-response.bin").read_bytes() == source
    assert (capture / "capture-manifest.json").is_file()
    assert (capture / "evaluation.json").is_file()
    assert not (capture / "structured-evidence.json").exists()


def test_non_success_response_preserves_capture_and_fails(tmp_path: Path) -> None:
    with pytest.raises(ProviderDataError, match="HTTP 503"):
        _collector(tmp_path).collect_response(
            _html(), captured_at=CAPTURED_AT, http_status=503
        )

    capture = next((tmp_path / "captures" / "premier-league-injuries").iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert (capture / "evaluation.json").is_file()
    assert not (capture / "structured-evidence.json").exists()
    evaluation = _load_json(capture / "evaluation.json")
    assert evaluation["collection_success"] is False
    assert evaluation["parse_success"] is False


def test_unexpected_content_type_preserves_capture_and_fails(tmp_path: Path) -> None:
    with pytest.raises(ProviderDataError, match="unexpected Premier League content type"):
        _collector(tmp_path).collect_response(
            _html(), captured_at=CAPTURED_AT, content_type="application/json"
        )

    capture = next((tmp_path / "captures" / "premier-league-injuries").iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert (capture / "evaluation.json").is_file()
    assert not (capture / "structured-evidence.json").exists()


def test_real_issue_57_validation_failure_cleans_candidate_and_final_output(
    tmp_path: Path,
) -> None:
    invalid_players = tuple(
        player.model_copy(
            update={
                "external_refs": (
                    ExternalRef(
                        provider="not_fpl_code",
                        external_id=player.external_refs[0].external_id,
                    ),
                )
            }
        )
        for player in _players()
    )

    with pytest.raises(ProviderDataError, match="emitted artefact failed"):
        _collector(tmp_path, canonical_players=invalid_players).collect_response(
            _html(), captured_at=CAPTURED_AT
        )

    capture = next((tmp_path / "captures" / "premier-league-injuries").iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert (capture / "evaluation.json").is_file()
    assert not (capture / "structured-evidence.json").exists()
    assert not (capture / ".structured-evidence.json.tmp").exists()
    evaluation = _load_json(capture / "evaluation.json")
    assert evaluation["collection_success"] is False
    assert evaluation["parse_success"] is True


def test_existing_capture_is_never_overwritten(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    first = collector.collect_response(_html(), captured_at=CAPTURED_AT)
    original = (first.path / "raw-response.bin").read_bytes()

    with pytest.raises(ProviderDataError, match="refusing to overwrite"):
        collector.collect_response(_html(), captured_at=CAPTURED_AT)
    assert (first.path / "raw-response.bin").read_bytes() == original


def test_emitted_artifact_is_consumed_by_real_issue_57_provider(tmp_path: Path) -> None:
    players = _players()
    result = _collector(tmp_path, canonical_players=players).collect_response(
        _html(), captured_at=CAPTURED_AT
    )
    assert result.structured_evidence_path is not None
    response = StructuredTeamNewsEvidenceProvider(
        result.structured_evidence_path,
        players,
        processed_at=CAPTURED_AT + timedelta(hours=1),
    ).evidence()

    assert len(response.data) == 3
    assert {item.source_external_player_id for item in response.data} == {"101", "102", "201"}
    assert all(item.source_provider == "fpl_code" for item in response.data)
    assert response.freshness.as_of == CAPTURED_AT
    assert response.provenance.source_sha256 == hashlib.sha256(
        result.structured_evidence_path.read_bytes()
    ).hexdigest()


def test_naive_capture_time_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="captured_at must be timezone-aware"):
        _collector(tmp_path).collect_response(
            _html(), captured_at=datetime(2026, 8, 19, 15, 0)
        )


def test_no_bootstrap_fails_mapping_without_emitting_evidence(tmp_path: Path) -> None:
    with pytest.raises(ProviderMappingError, match="bootstrap-static.json is required"):
        _collector(tmp_path, bootstrap_path=None).collect_response(
            _html(), captured_at=CAPTURED_AT
        )
    capture = next((tmp_path / "captures" / "premier-league-injuries").iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert not (capture / "structured-evidence.json").exists()
