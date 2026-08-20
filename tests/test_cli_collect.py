"""Deterministic CLI tests for the Premier League team-news collect command."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fpl_decision_engine.cli import app
from fpl_decision_engine.infrastructure.providers.team_news import (
    PremierLeagueCapture,
    PremierLeagueInjuriesCollector,
)
from fpl_decision_engine.ports import ProviderMappingError

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "team_news"
HTML_PATH = FIXTURE_ROOT / "premier_league_latest_injuries.html"
BOOTSTRAP_PATH = FIXTURE_ROOT / "bootstrap-static-team-news.json"
CAPTURED_AT = datetime(2026, 8, 19, 16, 0, tzinfo=UTC)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _offline_collect(self: PremierLeagueInjuriesCollector) -> PremierLeagueCapture:
    return self.collect_response(HTML_PATH.read_bytes(), captured_at=CAPTURED_AT)


def test_collect_help_exposes_bootstrap_and_output_args() -> None:
    result = CliRunner().invoke(app, ["collect", "--help"])

    assert result.exit_code == 0, result.output
    output = ANSI_ESCAPE_RE.sub("", result.output)
    assert "--bootstrap" in output
    assert "--output" in output


def test_collect_command_runs_offline_and_reports_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PremierLeagueInjuriesCollector, "collect", _offline_collect)
    result = CliRunner().invoke(
        app,
        ["collect", "--bootstrap", str(BOOTSTRAP_PATH), "--output", str(tmp_path / "captures")],
    )

    assert result.exit_code == 0, result.output
    assert "capture_id: " in result.output
    assert "capture_directory: " in result.output
    assert "structured_evidence: " in result.output
    assert result.output.count("structured-evidence.json") >= 1
    assert "evaluation: " in result.output
    assert "evaluation.json" in result.output

    capture_root = tmp_path / "captures" / "premier-league-injuries"
    capture = next(capture_root.iterdir())
    assert (capture / "raw-response.bin").is_file()
    assert (capture / "capture-manifest.json").is_file()
    assert (capture / "structured-evidence.json").is_file()
    assert (capture / "evaluation.json").is_file()


def test_collect_command_propagates_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_collect(self: PremierLeagueInjuriesCollector) -> PremierLeagueCapture:
        del self
        raise ProviderMappingError("identity mapping failed", provider_id="premier-league")

    monkeypatch.setattr(PremierLeagueInjuriesCollector, "collect", failing_collect)
    result = CliRunner().invoke(
        app,
        ["collect", "--bootstrap", str(BOOTSTRAP_PATH), "--output", str(tmp_path / "captures")],
    )

    assert result.exit_code == 1
    assert "error [mapping] provider=premier-league: identity mapping failed" in result.output
