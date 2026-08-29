"""CLI wiring for the Touchline run-record provenance ledger and doctor diagnostics.

Operators record and inspect run provenance through typed commands instead of manual
JSON edits; every mutation is validated and committed atomically by the ledger. The
``doctor`` command diagnoses environment, repository and state-root readiness
before a Gameweek run without mutating anything.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import UUID, uuid4

import typer

from fpl_decision_engine.application.analytical_artifacts import AnalyticalArtifactService
from fpl_decision_engine.application.analytical_history import (
    AnalyticalHistoryService,
)
from fpl_decision_engine.application.decision_bundles import load_decision_bundle
from fpl_decision_engine.application.doctor import DiagnosticStatus, DoctorService
from fpl_decision_engine.application.gameweek_evidence import (
    GameweekEvidenceArtifact,
    InvalidEvidenceManifest,
    load_gameweek_evidence_artifact,
    parse_gameweek_evidence_manifest,
)
from fpl_decision_engine.application.manager_state import (
    ManagerStateSource,
    acquire_manager_state,
)
from fpl_decision_engine.application.orchestration import (
    GameweekOrchestrator,
    OrchestratorError,
    OrchestratorRequest,
)
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain import (
    GameweekNumber,
    ManagerStateResult,
    ManagerStateSnapshot,
    Position,
)
from fpl_decision_engine.domain.run_record import (
    CloseOutcome,
    LegacyRunRecord,
    RunRecord,
    StageState,
)
from fpl_decision_engine.infrastructure.ingestion.snapshots import (
    PreparedSnapshot,
    RawSourceObject,
)
from fpl_decision_engine.infrastructure.orchestration import LocalBlankSquadBaselineRunner
from fpl_decision_engine.infrastructure.persistence.analytical_artifacts import (
    FileAnalyticalArtifactRepository,
)
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import map_snapshot
from fpl_decision_engine.infrastructure.providers.manager_state.fpl_api import (
    OfficialFplManagerStateSource,
)
from fpl_decision_engine.ports.analytical_artifacts import AnalyticalArtifactError
from fpl_decision_engine.ports.persistence import UnsupportedSchemaVersion
from fpl_decision_engine.ports.run_records import RunRecordError

app = typer.Typer(no_args_is_help=True, help="Touchline control-plane tooling")
run_record_app = typer.Typer(
    no_args_is_help=True, help="Typed, atomic run-record provenance ledger"
)
app.add_typer(run_record_app, name="run-record")


class _JsonHttpResponse:
    def __init__(self, *, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _UrlopenHttp:
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> _JsonHttpResponse:
        request = UrlRequest(url, headers=dict(headers))
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return _JsonHttpResponse(
                status_code=response.status,
                payload=json.loads(response.read().decode("utf-8")),
            )


@dataclass(frozen=True, slots=True)
class _VerifiedManagerStateRunner:
    source: ManagerStateSource
    known_player_types: dict[int, Position]

    def acquire(self, *, entry_id: int, target_event: GameweekNumber) -> ManagerStateResult:
        return acquire_manager_state(
            self.source,
            entry_id=entry_id,
            target_event=target_event,
            known_player_types=self.known_player_types,
        )


def _submission_identity_maps(
    artifact: GameweekEvidenceArtifact,
) -> tuple[dict[int, Position], dict[UUID, int], dict[int, UUID]]:
    manifest = parse_gameweek_evidence_manifest(artifact.read_bytes())
    bootstrap = Path(manifest.snapshot.bootstrap.reference).read_bytes()
    fixtures = Path(manifest.snapshot.fixtures.reference).read_bytes()
    canonical = map_snapshot(
        PreparedSnapshot(
            provider_id=manifest.snapshot.provider_id,
            observed_at=manifest.snapshot.observed_at,
            season=manifest.season,
            requested_snapshot_id=manifest.snapshot.snapshot_id,
            objects=(
                RawSourceObject(
                    resource_name="bootstrap-static",
                    original_filename=Path(manifest.snapshot.bootstrap.reference).name,
                    data=bootstrap,
                    sha256=manifest.snapshot.bootstrap.sha256,
                ),
                RawSourceObject(
                    resource_name="fixtures",
                    original_filename=Path(manifest.snapshot.fixtures.reference).name,
                    data=fixtures,
                    sha256=manifest.snapshot.fixtures.sha256,
                ),
            ),
        )
    )
    player_element_ids: dict[UUID, int] = {}
    known_player_types: dict[int, Position] = {}
    for player in canonical.players:
        refs = tuple(
            ref
            for ref in player.external_refs
            if ref.provider == manifest.snapshot.provider_id
        )
        if len(refs) != 1:
            raise ValueError(
                f"canonical player {player.id} must have exactly one "
                f"{manifest.snapshot.provider_id} element reference"
            )
        element_id = int(refs[0].external_id)
        player_element_ids[player.id] = element_id
        known_player_types[element_id] = player.position
    if len(set(player_element_ids.values())) != len(player_element_ids):
        raise ValueError("canonical player to FPL element mapping is ambiguous")
    element_player_ids = {value: key for key, value in player_element_ids.items()}
    return known_player_types, player_element_ids, element_player_ids


@app.command("doctor")
def doctor_command(
    state_root: Annotated[
        Path, typer.Option("--state-root", help="State root to diagnose.")
    ] = Path("state/run-records"),
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the report as machine-readable JSON instead of human output.",
        ),
    ] = False,
) -> None:
    """Diagnose environment, repository and state-root readiness before a run.

    Read-only and deterministic: every check prints an explicit PASS/WARN/FAIL with
    remediation for failures. Exit status is 0 only when no check failed.
    """
    report = DoctorService(cwd=Path.cwd(), state_root=state_root).run()
    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for check in report.checks:
            typer.echo(f"{check.status.value}  {check.identifier}  {check.message}")
            if check.remediation is not None:
                typer.echo(f"      remediation: {check.remediation}")
        failed = sum(1 for check in report.checks if check.status is DiagnosticStatus.FAIL)
        typer.echo(f"doctor: {len(report.checks)} checks, {failed} failed")
    raise typer.Exit(code=0 if report.ok else 1)


@app.command("run-gameweek")
def run_gameweek_command(
    season: Annotated[str, typer.Option(help="Season, e.g. 2026-27.")],
    gameweek: Annotated[int, typer.Option(help="Gameweek number.")],
    evidence_manifest: Annotated[
        Path,
        typer.Option(
            "--evidence-manifest",
            help="Persisted immutable Gameweek evidence manifest.",
        ),
    ],
    code_revision: Annotated[
        str, typer.Option(help="Exact code revision for reproducible resume.")
    ],
    config_fingerprint: Annotated[
        str,
        typer.Option(help="Effective baseline configuration fingerprint."),
    ],
    state_root: Annotated[
        Path,
        typer.Option(help="Operational state root containing run-records and artifacts."),
    ] = Path("state"),
    resume: Annotated[
        UUID | None,
        typer.Option(help="Existing provisional run ID to resume."),
    ] = None,
    run_id: Annotated[
        UUID | None,
        typer.Option(help="Optional explicit ID for a fresh run."),
    ] = None,
    fpl_entry_id: Annotated[
        int | None,
        typer.Option(help="Authenticated FPL manager entry id for submission safety."),
    ] = None,
    operator: Annotated[
        str | None,
        typer.Option(help="Operator identity for external FPL execution confirmation."),
    ] = None,
    confirm_operator_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-operator-execution",
            help="Record that the operator reports external FPL action was attempted/completed.",
        ),
    ] = False,
    previous_manager_state: Annotated[
        Path | None,
        typer.Option(help="Optional explicit previous verified manager-state artefact."),
    ] = None,
    previous_state_acknowledged: Annotated[
        bool,
        typer.Option(help="Acknowledge an explicit previous/current manager-state difference."),
    ] = False,
) -> None:
    """Execute or explicitly resume doctor → evidence → baseline."""

    if resume is not None and run_id is not None:
        typer.echo("error: --run-id cannot be combined with --resume", err=True)
        raise typer.Exit(code=2)
    selected_run_id = resume or run_id or uuid4()
    try:
        if fpl_entry_id is None:
            raise ValueError("--fpl-entry-id is required for submission safety")
        fpl_cookie = os.environ.get("FPL_COOKIE")
        if not fpl_cookie:
            raise ValueError(
                "FPL_COOKIE runtime environment value is required for submission safety"
            )
        if confirm_operator_execution and not operator:
            raise ValueError("--operator is required with --confirm-operator-execution")
        artifact = load_gameweek_evidence_artifact(evidence_manifest)
        known_player_types, player_element_ids, element_player_ids = _submission_identity_maps(
            artifact
        )
        previous_verified = (
            ManagerStateSnapshot.model_validate_json(previous_manager_state.read_bytes())
            if previous_manager_state is not None
            else None
        )
        records = RunRecordService(RunRecordLedger(state_root / "run-records"))
        manager_state = _VerifiedManagerStateRunner(
            OfficialFplManagerStateSource(
                _UrlopenHttp(),
                base_url="https://fantasy.premierleague.com",
                headers={"Cookie": fpl_cookie},
            ),
            known_player_types,
        )
        orchestrator = GameweekOrchestrator(
            records,
            doctor=DoctorService(cwd=Path.cwd(), state_root=state_root),
            baseline=LocalBlankSquadBaselineRunner(state_root=state_root),
            manager_state=manager_state,
            state_root=state_root,
            decision_loader=load_decision_bundle,
        )
        result = orchestrator.run(
            OrchestratorRequest(
                run_id=selected_run_id,
                season=season,
                gameweek=gameweek,
                code_revision=code_revision,
                config_fingerprint=config_fingerprint,
                evidence_artifact=artifact,
                expected_entry_id=fpl_entry_id,
                player_element_ids=player_element_ids,
                element_player_ids=element_player_ids,
                previous_verified_manager_state=previous_verified,
                previous_state_acknowledged=previous_state_acknowledged,
                operator=operator,
                operator_execution_confirmed=confirm_operator_execution,
                resume=resume is not None,
            )
        )
    except (
        InvalidEvidenceManifest,
        OSError,
        OrchestratorError,
        RunRecordError,
        UnsupportedSchemaVersion,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"run_id: {result.record.run_id}")
    typer.echo(f"state: {result.record.state.value}")
    for stage, status in result.stage_statuses:
        typer.echo(f"stage {stage}: {status.value if status is not None else 'unattempted'}")
    typer.echo(f"next_action: {result.next_action}")
    if result.recommendation is not None:
        typer.echo(f"baseline_objective: {result.recommendation.primary_objective:.6f}")
    raise typer.Exit(code=result.exit_code)


@app.command("analytics")
def analytics_command(
    run_id: Annotated[UUID, typer.Argument(help="Completed run record id.")],
    state_root: Annotated[
        Path,
        typer.Option(help="Operational state root containing run-records and artifacts."),
    ] = Path("state"),
) -> None:
    """Generate downstream history/comparison analytical artefacts for a completed run.

    Read-only for the run record: previous state is resolved exclusively from
    ``previous_run_id`` and analytical failure surfaces as explicit analytical
    WARN/error output, never reopening, downgrading or mutating the completed run.
    """
    try:
        service = AnalyticalHistoryService(
            records=RunRecordService(RunRecordLedger(state_root / "run-records")),
            bundle_loader=load_decision_bundle,
            analytical=AnalyticalArtifactService(
                FileAnalyticalArtifactRepository(state_root)
            ),
        )
        result = service.generate(run_id=run_id)
    except (AnalyticalArtifactError, RunRecordError, OSError) as exc:
        typer.echo(f"analytical error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"run_id: {run_id}")
    typer.echo(f"history: {result.history.analysis_artifact_id}")
    if result.comparison is None:
        typer.echo("comparison: none (no previous run)")
        return
    typer.echo(f"comparison: {result.comparison.analysis_artifact_id}")
    typer.echo(
        f"classification: {result.change.value if result.change is not None else 'n/a'}"
    )


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
