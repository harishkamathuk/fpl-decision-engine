from __future__ import annotations

import inspect
import json
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from fpl_decision_engine.touchline_cli import app, run_gameweek_command

runner = CliRunner()


def invoke(*args: str) -> object:
    return runner.invoke(app, ["run-record", *args])


def test_run_record_cli_create_stage_close_flow(tmp_path: Path) -> None:
    result = invoke(
        "--state-root",
        str(tmp_path),
        "create",
        "--season",
        "2026-27",
        "--gameweek",
        "1",
        "--mandatory-stage",
        "ingest",
        "--mandatory-stage",
        "optimise",
    )
    assert result.exit_code == 0, result.output
    assert "state: provisional" in result.output
    run_id = result.output.split("run_id: ")[1].split()[0]

    result = invoke("--state-root", str(tmp_path), "stage", run_id, "ingest", "--status", "running")
    assert result.exit_code == 0, result.output
    assert "running" in result.output

    result = invoke("--state-root", str(tmp_path), "stage", run_id, "ingest", "--status", "pass")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output

    result = invoke(
        "--state-root", str(tmp_path), "stage", run_id, "optimise", "--status", "running"
    )
    assert result.exit_code == 0, result.output
    result = invoke("--state-root", str(tmp_path), "stage", run_id, "optimise", "--status", "pass")
    assert result.exit_code == 0, result.output

    result = invoke("--state-root", str(tmp_path), "close", run_id, "--outcome", "completed")
    assert result.exit_code == 0, result.output
    assert "completed" in result.output

    result = invoke("--state-root", str(tmp_path), "show", run_id)
    assert result.exit_code == 0, result.output
    assert "state: completed" in result.output
    assert "stage ingest: pass (attempt 1)" in result.output

    result = invoke("--state-root", str(tmp_path), "validate", run_id)
    assert result.exit_code == 0, result.output
    assert "valid" in result.output

    assert (Path(tmp_path) / f"{run_id}.json").is_file()


def test_run_record_cli_summary_text_and_json(tmp_path: Path) -> None:
    result = invoke(
        "--state-root",
        str(tmp_path),
        "create",
        "--season",
        "2026-27",
        "--gameweek",
        "1",
        "--mandatory-stage",
        "ingest",
    )
    assert result.exit_code == 0, result.output
    run_id = result.output.split("run_id: ")[1].split()[0]

    text = invoke("--state-root", str(tmp_path), "summary", run_id)
    assert text.exit_code == 0, text.output
    assert "trigger: not-recorded" in text.output
    assert "scenario_status" not in text.output

    machine = invoke("--state-root", str(tmp_path), "summary", run_id, "--json")
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["run"]["run_id"] == run_id
    assert payload["run"]["trigger"] == "not-recorded"
    assert payload["run"]["scenario_status"] == "not-recorded"


def test_run_record_cli_invalid_previous_run_rejected(tmp_path: Path) -> None:
    missing = uuid4()
    result = invoke(
        "--state-root",
        str(tmp_path),
        "create",
        "--season",
        "2026-27",
        "--gameweek",
        "1",
        "--mandatory-stage",
        "ingest",
        "--previous-run-id",
        str(missing),
    )
    assert result.exit_code == 1
    assert "does not reference an existing run record" in result.output
    assert str(missing) in result.output


def test_run_record_cli_invalid_transition_rejected(tmp_path: Path) -> None:
    result = invoke(
        "--state-root",
        str(tmp_path),
        "create",
        "--season",
        "2026-27",
        "--gameweek",
        "1",
        "--mandatory-stage",
        "ingest",
    )
    run_id = result.output.split("run_id: ")[1].split()[0]

    result = invoke("--state-root", str(tmp_path), "stage", run_id, "ingest", "--status", "pass")
    assert result.exit_code == 1
    assert "only a RUNNING attempt may finish" in result.output


def test_run_record_cli_validate_legacy_reports_issues(tmp_path: Path) -> None:
    run_id = uuid4()
    (Path(tmp_path) / f"{run_id}.json").write_text(
        json.dumps({"run_id": str(run_id), "season": "2026-27", "gameweek": 1}),
        encoding="utf-8",
    )

    result = invoke("--state-root", str(tmp_path), "validate", str(run_id))

    assert result.exit_code == 1
    assert "legacy" in result.output


def test_run_record_cli_promote_requires_completed(tmp_path: Path) -> None:
    result = invoke(
        "--state-root",
        str(tmp_path),
        "create",
        "--season",
        "2026-27",
        "--gameweek",
        "1",
        "--mandatory-stage",
        "ingest",
    )
    run_id = result.output.split("run_id: ")[1].split()[0]

    result = invoke(
        "--state-root",
        str(tmp_path),
        "promote",
        run_id,
        "--by",
        "operator",
        "--reason",
        "final",
    )
    assert result.exit_code == 1
    assert "only a completed run may become authoritative" in result.output


def test_run_gameweek_cli_exposes_explicit_resume_and_submission_contract() -> None:
    sig = inspect.signature(run_gameweek_command)
    expected_params = {
        "evidence_manifest",
        "code_revision",
        "config_fingerprint",
        "state_root",
        "resume",
        "run_id",
        "fpl_entry_id",
        "operator",
        "confirm_operator_execution",
        "previous_manager_state",
        "previous_state_acknowledged",
    }
    actual_params = set(sig.parameters.keys())
    missing = expected_params - actual_params
    extra = actual_params - expected_params - {"season", "gameweek"}
    assert not missing, f"Missing parameters: {missing}"
    assert not extra, f"Unexpected parameters: {extra}"


def test_run_gameweek_cli_does_not_accept_raw_cookie_argument() -> None:
    sig = inspect.signature(run_gameweek_command)
    assert "fpl_cookie" not in sig.parameters
    assert "--fpl-cookie" not in runner.invoke(app, ["run-gameweek", "--help"]).output


def test_analytics_cli_surfaces_analytical_failure_as_error_output(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["analytics", str(uuid4()), "--state-root", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "analytical error:" in result.output
