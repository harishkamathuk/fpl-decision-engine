"""CLI wiring for the Touchline run-record provenance ledger.

Operators record and inspect run provenance through typed commands instead of manual
JSON edits; every mutation is validated and committed atomically by the ledger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID, uuid4

import typer

from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain.run_record import (
    CloseOutcome,
    LegacyRunRecord,
    RunRecord,
    StageState,
)
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger
from fpl_decision_engine.ports.run_records import RunRecordError

app = typer.Typer(no_args_is_help=True, help="Touchline control-plane tooling")
run_record_app = typer.Typer(
    no_args_is_help=True, help="Typed, atomic run-record provenance ledger"
)
app.add_typer(run_record_app, name="run-record")


def _run_record_options(
    ctx: typer.Context,
    state_root: Annotated[
        Path, typer.Option("--state-root", help="Run-record ledger root directory.")
    ] = Path("state/run-records"),
) -> None:
    ctx.obj = RunRecordService(RunRecordLedger(state_root))


run_record_app.callback()(_run_record_options)


def _service(ctx: typer.Context) -> RunRecordService:
    return ctx.obj


def _report(error: RunRecordError) -> NoReturn:
    typer.echo(f"error: {error}", err=True)
    raise typer.Exit(code=1)


def _echo_run_line(record: RunRecord | LegacyRunRecord) -> None:
    state = record.state if isinstance(record, LegacyRunRecord) else record.state.value
    typer.echo(
        f"run_id: {record.run_id}  season: {record.season}  gameweek: {record.gameweek}  "
        f"state: {state}  format: {'legacy' if isinstance(record, LegacyRunRecord) else 'v1'}"
    )


def _echo_summary(record: RunRecord | LegacyRunRecord) -> None:
    typer.echo(f"run_id: {record.run_id}")
    typer.echo(f"format: {'legacy' if isinstance(record, LegacyRunRecord) else 'v1'}")
    typer.echo(f"season: {record.season}")
    typer.echo(f"gameweek: {record.gameweek}")
    state = record.state if isinstance(record, LegacyRunRecord) else record.state.value
    typer.echo(f"state: {state}")
    typer.echo(f"previous_run_id: {record.previous_run_id}")
    typer.echo(f"created_at: {record.created_at}")
    typer.echo(f"closed_at: {record.closed_at}")
    typer.echo(f"code_revision: {record.code_revision}")
    typer.echo(f"config_fingerprint: {record.config_fingerprint}")
    if isinstance(record, LegacyRunRecord):
        typer.echo(f"mandatory_stages: {', '.join(record.mandatory_stages) or 'unknown'}")
        typer.echo(f"stage_attempts: {len(record.stage_attempts)}")
        typer.echo(f"artefacts: {len(record.artefacts)}")
        typer.echo(f"decisions: {len(record.decisions)}")
        if record.parse_issues:
            typer.echo("parse_issues:")
            for issue in record.parse_issues:
                typer.echo(f"  - {issue}")
        typer.echo(
            "note: legacy record without schema_version 1; absent fields are unknown and "
            "were not fabricated"
        )
        return
    for stage in record.mandatory_stages:
        latest = record.latest_attempt(stage)
        if latest is None:
            typer.echo(f"stage {stage}: unattempted")
        else:
            by = f" by {latest.by}" if latest.by else ""
            typer.echo(f"stage {stage}: {latest.status.value} (attempt {latest.attempt}){by}")
    typer.echo(f"artefacts: {len(record.artefacts)}")
    for artefact in record.artefacts:
        typer.echo(
            f"  artefact {artefact.name}: {artefact.reference} sha256={artefact.sha256}"
        )
    typer.echo(f"decisions: {len(record.decisions)}")
    for decision in record.decisions:
        typer.echo(
            f"  decision: {decision.reference}"
            f"{f' sha256={decision.sha256}' if decision.sha256 else ''}"
        )
    if record.authority_events:
        event = record.authority_events[0]
        typer.echo(
            f"authority: approved by {event.by} at {event.approved_at.isoformat()} — "
            f"{event.reason}"
        )
    else:
        typer.echo("authority: none")
    if record.diagnostic_summary:
        typer.echo(f"diagnostic_summary: {record.diagnostic_summary}")


@run_record_app.command("create")
def create_command(
    ctx: typer.Context,
    season: Annotated[str, typer.Option(help="Season, e.g. 2026-27.")],
    gameweek: Annotated[int, typer.Option(help="Gameweek number.")],
    mandatory_stage: Annotated[
        list[str], typer.Option(help="Mandatory stage name; repeatable.")
    ],
    run_id: Annotated[UUID | None, typer.Option(help="Run id; defaults to a fresh UUID.")] = None,
    previous_run_id: Annotated[
        UUID | None,
        typer.Option(
            help="Explicit previous run id; defaults to deterministic resolution of the "
            "current authoritative run."
        ),
    ] = None,
    code_revision: Annotated[str | None, typer.Option(help="Code revision.")] = None,
    config_fingerprint: Annotated[
        str | None, typer.Option(help="Effective configuration fingerprint.")
    ] = None,
) -> None:
    """Create a provisional run with validated lineage."""
    service = _service(ctx)
    try:
        record = service.create_run(
            run_id=run_id or uuid4(),
            season=season,
            gameweek=gameweek,
            mandatory_stages=mandatory_stage,
            previous_run_id=previous_run_id,
            code_revision=code_revision,
            config_fingerprint=config_fingerprint,
        )
    except RunRecordError as exc:
        _report(exc)
    _echo_run_line(record)


@run_record_app.command("show")
def show_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
) -> None:
    """Read and validate one run record."""
    service = _service(ctx)
    try:
        record = service.get_run(run_id)
    except RunRecordError as exc:
        _report(exc)
    _echo_summary(record)


@run_record_app.command("list")
def list_command(
    ctx: typer.Context,
    season: Annotated[str | None, typer.Option(help="Filter by season.")] = None,
    gameweek: Annotated[int | None, typer.Option(help="Filter by gameweek.")] = None,
) -> None:
    """List recorded runs, optionally filtered."""
    service = _service(ctx)
    try:
        records = service.list_runs(season=season, gameweek=gameweek)
    except RunRecordError as exc:
        _report(exc)
    if not records:
        typer.echo("no run records")
        return
    for record in records:
        _echo_run_line(record)


@run_record_app.command("stage")
def stage_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
    stage: Annotated[str, typer.Argument(help="Stage name.")],
    status: Annotated[
        StageState,
        typer.Option(
            help="Target stage state: running, pass, warn, fail, blocked, or pending "
            "(approved retry creating a new attempt)."
        ),
    ],
    by: Annotated[
        str | None, typer.Option(help="Operator attribution; required for retries.")
    ] = None,
    note: Annotated[str | None, typer.Option(help="Optional note.")] = None,
) -> None:
    """Record a stage result through an approved transition."""
    service = _service(ctx)
    try:
        if status is StageState.RUNNING:
            record = service.start_stage(run_id, stage, by=by, note=note)
        elif status is StageState.PENDING:
            record = service.retry_stage(run_id, stage, by=by, note=note)
        elif status is StageState.BLOCKED:
            record = service.block_stage(run_id, stage, by=by, note=note)
        else:
            record = service.finish_stage(run_id, stage, status, by=by, note=note)
    except RunRecordError as exc:
        _report(exc)
    latest = record.latest_attempt(stage)
    current = latest.status.value if latest is not None else "unknown"
    typer.echo(f"run {run_id}: stage '{stage}' is now {current}")


@run_record_app.command("artefact")
def artefact_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
    name: Annotated[str, typer.Option(help="Artefact name.")],
    reference: Annotated[str, typer.Option(help="Artefact reference/path.")],
    sha256: Annotated[str, typer.Option(help="Lowercase SHA-256 digest.")],
    kind: Annotated[str | None, typer.Option(help="Optional artefact kind.")] = None,
) -> None:
    """Record an artefact reference and content hash."""
    service = _service(ctx)
    try:
        record = service.record_artefact(
            run_id, name=name, reference=reference, sha256=sha256, kind=kind
        )
    except RunRecordError as exc:
        _report(exc)
    typer.echo(f"run {run_id}: recorded artefact '{name}' (artefacts={len(record.artefacts)})")


@run_record_app.command("decision")
def decision_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
    reference: Annotated[str, typer.Option(help="Decision reference (e.g. bundle path).")],
    sha256: Annotated[str | None, typer.Option(help="Optional lowercase SHA-256 digest.")] = None,
    by: Annotated[str | None, typer.Option(help="Optional attribution.")] = None,
    summary: Annotated[str | None, typer.Option(help="Optional summary.")] = None,
) -> None:
    """Record a decision reference."""
    service = _service(ctx)
    try:
        record = service.record_decision(
            run_id, reference=reference, sha256=sha256, by=by, summary=summary
        )
    except RunRecordError as exc:
        _report(exc)
    typer.echo(f"run {run_id}: recorded decision (decisions={len(record.decisions)})")


@run_record_app.command("close")
def close_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
    outcome: Annotated[CloseOutcome, typer.Option(help="completed or failed.")],
    by: Annotated[str | None, typer.Option(help="Optional operator attribution.")] = None,
    note: Annotated[str | None, typer.Option(help="Optional diagnostic summary.")] = None,
) -> None:
    """Close a provisional run as completed or failed."""
    service = _service(ctx)
    try:
        record = service.close_run(run_id, outcome=outcome, by=by, note=note)
    except RunRecordError as exc:
        _report(exc)
    typer.echo(f"run {run_id}: state is now {record.state.value}")


@run_record_app.command("promote")
def promote_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
    by: Annotated[str, typer.Option(help="Operator approving promotion (required).")],
    reason: Annotated[str, typer.Option(help="Approval reason (required).")],
) -> None:
    """Promote a completed run to authoritative with explicit operator approval."""
    service = _service(ctx)
    try:
        service.promote_run(run_id, by=by, reason=reason)
    except RunRecordError as exc:
        _report(exc)
    typer.echo(f"run {run_id}: promoted to authoritative")


@run_record_app.command("validate")
def validate_command(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(help="Run record id.")],
) -> None:
    """Validate that an existing run record reads back consistently."""
    service = _service(ctx)
    try:
        report = service.validate_run(run_id)
    except RunRecordError as exc:
        _report(exc)
    if report.ok:
        typer.echo(f"run {run_id}: valid")
        return
    typer.echo(f"run {run_id}: issues found", err=True)
    for issue in report.issues:
        typer.echo(f"  - {issue}", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
