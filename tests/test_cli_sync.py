from __future__ import annotations

import shutil
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fpl_decision_engine.cli import app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "fpl_snapshot"


def test_sync_command_runs_offline_and_reports_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input"
    shutil.copytree(FIXTURE_ROOT, source)

    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in snapshot tests")

    monkeypatch.setattr(socket, "socket", network_forbidden)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        ["sync", "--source", "snapshot", "--input", str(source)],
    )

    assert result.exit_code == 0, result.output
    assert "provider_id: synthetic-fpl" in result.output
    assert "mapped: gameweeks=1 teams=2 players=2 fixtures=1" in result.output
    assert "warnings: 1" in result.output
    assert "import: created" in result.output


def test_sync_command_rejects_non_snapshot_source() -> None:
    result = CliRunner().invoke(
        app,
        ["sync", "--source", "refresh", "--input", "unused"],
    )

    assert result.exit_code != 0
    assert "only the offline 'snapshot' source" in result.output
