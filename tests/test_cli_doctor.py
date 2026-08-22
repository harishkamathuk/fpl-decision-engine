"""CLI tests for `touchline doctor` using controlled temporary git repositories.

The smoke tests run in a checked-out working directory, so they exercise the real
toolchain (git, uv, Python 3.12) exactly as the project's own CI does.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from fpl_decision_engine.touchline_cli import app

runner = CliRunner()

CHECK_COUNT = 12


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", ".")
    assert _git(repo, "commit", "-qm", "initial").returncode == 0
    (repo / "data").mkdir()
    (repo / "configs").mkdir()
    return repo


def test_doctor_cli_healthy_exit_zero(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "state").mkdir()
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["doctor", "--state-root", "state"])

    assert result.exit_code == 0, result.output
    assert f"doctor: {CHECK_COUNT} checks, 0 failed" in result.output
    assert "PASS  git.worktree_clean" in result.output
    assert "PASS  paths.state_root_placement" in result.output
    assert "FAIL" not in result.output


def test_doctor_cli_release_worktree_failure_exit_one(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _git(repo, "tag", "v1.0.0")
    _git(repo, "checkout", "-q", "v1.0.0")
    (repo / "state").mkdir()
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["doctor", "--state-root", "state"])

    assert result.exit_code == 1
    assert "FAIL  paths.state_root_placement" in result.output
    assert "release tag: v1.0.0" in result.output
    assert "remediation:" in result.output


def test_doctor_cli_not_a_repository_exit_one(monkeypatch, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL  git.repository" in result.output


def test_doctor_cli_json_output(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "state").mkdir()
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["doctor", "--state-root", "state", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    checks = payload["checks"]
    assert len(checks) == CHECK_COUNT
    for check in checks:
        assert set(check) == {"identifier", "status", "message", "remediation"}
        assert check["status"] in {"PASS", "WARN", "FAIL"}
    assert [check["identifier"] for check in checks] == [
        "tools.git",
        "git.repository",
        "git.commit_sha",
        "git.release_tag",
        "git.worktree_clean",
        "paths.state_root_resolution",
        "paths.state_root_exists",
        "paths.state_root_placement",
        "dirs.required",
        "tools.uv",
        "python.version",
        "config.default_file",
    ]


def test_doctor_cli_output_is_deterministic(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "state").mkdir()
    monkeypatch.chdir(repo)

    first = runner.invoke(app, ["doctor", "--state-root", "state"])
    second = runner.invoke(app, ["doctor", "--state-root", "state"])

    assert first.exit_code == second.exit_code == 0
    assert first.output == second.output
