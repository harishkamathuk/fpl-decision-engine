"""#90 operational-docs tests: documented commands must stay valid and the
manual runbook must remain archived and marked superseded."""

from __future__ import annotations

import re
from pathlib import Path

from fpl_decision_engine.cli import app as fpl_app
from fpl_decision_engine.touchline_cli import app

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS_DIR = ROOT / "docs" / "operations"
ARCHIVE_RUNBOOK = ROOT / "docs" / "archive" / "gw1-operational-runbook.md"
LEGACY_RUNBOOK = ROOT / "docs" / "gw1-operational-runbook.md"

# Command paths that the operations docs are allowed to invoke, derived from the
# real CLI surface (subcommands resolved from the typer registered commands).
_TOUCHLINE_COMMANDS = {
    command.name for command in app.registered_commands
} | {group.name for group in app.registered_groups}
_RUN_RECORD_COMMANDS = {
    subcommand.name
    for group in app.registered_groups
    if group.name == "run-record"
    for subcommand in group.typer_instance.registered_commands
}
_FPL_COMMANDS = {name for name in (command.name for command in fpl_app.registered_commands) if name}

_FENCE = re.compile(r"^```")


def _documented_command_paths() -> list[tuple[str, list[str]]]:
    """Extract (runner, [command tokens]) from every fenced code block in the docs.

    Lines like ``uv run touchline run-record summary <run-id>`` yield
    ("touchline", ["run-record", "summary"]); option flags and values are ignored.
    """
    found: list[tuple[str, list[str]]] = []
    for markdown in sorted(OPERATIONS_DIR.glob("*.md")):
        in_fence = False
        for line in markdown.read_text(encoding="utf-8").splitlines():
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            stripped = line.strip()
            if stripped.startswith("uv run touchline"):
                tokens = stripped.split()[3:]  # drop `uv run touchline`
                found.append(("touchline", tokens))
            elif stripped.startswith("uv run fpl"):
                tokens = stripped.split()[3:]  # drop `uv run fpl`
                found.append(("fpl", tokens))
    return found


def _command_path(runner: str, tokens: list[str]) -> list[str]:
    """Resolve the leading command tokens that name a real command path."""
    if runner == "fpl":
        if tokens and tokens[0] in _FPL_COMMANDS:
            return [tokens[0]]
        return []
    if not tokens:
        return []
    first = tokens[0]
    if first not in _TOUCHLINE_COMMANDS:
        return []
    if first != "run-record":
        return [first]
    if len(tokens) > 1 and tokens[1] in _RUN_RECORD_COMMANDS:
        return ["run-record", tokens[1]]
    return ["run-record"]


def test_operations_docs_only_document_real_cli_commands() -> None:
    documented = _documented_command_paths()
    assert documented, "operations docs must contain fenced CLI examples"
    unresolved: list[str] = []
    for runner, tokens in documented:
        path = _command_path(runner, tokens)
        if not path:
            unresolved.append(f"{runner}: {' '.join(tokens)}")
    assert not unresolved, f"documented commands are not registered CLI commands: {unresolved}"


def test_quickstart_documented_command_paths_resolve(tmp_path: Path) -> None:
    # Every documented path must also resolve to an actual --help invocation, proving
    # the quickstart examples remain executable against the current CLI surface.
    from typer.testing import CliRunner

    touchline_runner = CliRunner()
    fpl_runner = CliRunner()
    failures: list[str] = []
    for runner, tokens in _documented_command_paths():
        path = _command_path(runner, tokens)
        if not path:
            continue
        target = touchline_runner if runner == "touchline" else fpl_runner
        result = target.invoke(_app_for(runner), [*path, "--help"])
        if result.exit_code != 0:
            failures.append(f"{runner} {' '.join(path)} -> exit {result.exit_code}")
    assert not failures, f"documented command paths failed --help: {failures}"


def _app_for(runner: str):
    return app if runner == "touchline" else fpl_app


def test_manual_runbook_archived_and_marked_superseded() -> None:
    assert LEGACY_RUNBOOK.exists() is False, (
        "the manual runbook must not remain at the active docs/gw1-operational-runbook.md path"
    )
    assert ARCHIVE_RUNBOOK.exists(), "the manual runbook must be archived, not deleted"
    content = ARCHIVE_RUNBOOK.read_text(encoding="utf-8")
    assert "SUPERSEDED" in content
    assert "operations/README.md" in content


def test_quickstart_covers_operator_workflow_topics() -> None:
    readme = (OPERATIONS_DIR / "README.md").read_text(encoding="utf-8")
    for topic in (
        "doctor",
        "run-gameweek",
        "--resume",
        "run-record summary",
        "promote",
        "submission-safety",
    ):
        assert topic in readme, f"quickstart must cover operator topic: {topic}"
