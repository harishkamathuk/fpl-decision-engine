"""End-to-end parity for the local #84 blank-squad baseline adapter."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fpl_decision_engine.application.doctor import (
    DiagnosticCheck,
    DiagnosticStatus,
    DoctorReport,
)
from fpl_decision_engine.application.gameweek_evidence import (
    ProjectionEvidenceInput,
    SnapshotEvidenceInput,
    build_gameweek_evidence_manifest,
    write_gameweek_evidence_manifest,
)
from fpl_decision_engine.application.orchestration import (
    GameweekOrchestrator,
    OrchestratorRequest,
)
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain import GameweekNumber, SingleGameweekOptimisationRequest
from fpl_decision_engine.infrastructure.ingestion.snapshots import (
    PreparedSnapshot,
    RawSourceObject,
)
from fpl_decision_engine.infrastructure.optimisation import HighsSingleGameweekOptimiser
from fpl_decision_engine.infrastructure.orchestration import LocalBlankSquadBaselineRunner
from fpl_decision_engine.infrastructure.persistence import DuckDbDecisionRunRepository
from fpl_decision_engine.infrastructure.persistence.run_records import RunRecordLedger
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import map_snapshot
from fpl_decision_engine.infrastructure.providers.projections import FplForecastCsvAdapter

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class PassDoctor:
    def run(self) -> DoctorReport:
        return DoctorReport(
            (
                DiagnosticCheck(
                    identifier="controlled.pass",
                    status=DiagnosticStatus.PASS,
                    message="controlled pass",
                ),
            )
        )


def _write_baseline_evidence(tmp_path: Path) -> tuple[bytes, bytes, Path]:
    counts = {1: 3, 2: 7, 3: 7, 4: 5}
    teams = [
        {"id": value, "name": f"Team {value}", "short_name": f"T{value}"}
        for value in range(1, 9)
    ]
    elements: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    number = 1
    for position, count in counts.items():
        for _ in range(count):
            code = 500_000 + number
            elements.append(
                {
                    "id": number,
                    "code": code,
                    "team": ((number - 1) % 8) + 1,
                    "first_name": f"First{number}",
                    "second_name": f"Last{number}",
                    "web_name": f"P{number}",
                    "element_type": position,
                    "now_cost": {1: 40, 2: 45, 3: 55, 4: 60}[position],
                    "status": "a",
                    "news": "",
                    "chance_of_playing_next_round": 100,
                }
            )
            rows.append(
                {
                    "schema_version": "phase9_frontend_v1",
                    "season": "2026-27",
                    "gameweek": 1,
                    "stable_player_id": f"player_code_{code}",
                    "expected_points": 20 - number * 0.4,
                    "model_variant": "baseline",
                    "data_timestamp": NOW.isoformat(),
                }
            )
            number += 1
    bootstrap = (
        json.dumps(
            {
                "events": [
                    {
                        "id": 1,
                        "name": "Gameweek 1",
                        "deadline_time": "2026-08-29T14:00:00Z",
                        "finished": False,
                    }
                ],
                "teams": teams,
                "elements": elements,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    fixtures = b"[]\n"
    component_root = tmp_path / "components"
    component_root.mkdir()
    bootstrap_path = component_root / "bootstrap-static.json"
    fixtures_path = component_root / "fixtures.json"
    projection_path = component_root / "projections.csv"
    bootstrap_path.write_bytes(bootstrap)
    fixtures_path.write_bytes(fixtures)
    with projection_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return bootstrap, fixtures, projection_path


def test_orchestrated_baseline_matches_existing_manual_path(tmp_path: Path) -> None:
    bootstrap, fixtures, projection_path = _write_baseline_evidence(tmp_path)
    component_root = projection_path.parent
    prepared = PreparedSnapshot(
        provider_id="fpl",
        observed_at=NOW,
        season="2026-27",
        objects=(
            RawSourceObject(
                resource_name="bootstrap-static",
                original_filename="bootstrap-static.json",
                data=bootstrap,
                sha256="unused-by-mapper",
            ),
            RawSourceObject(
                resource_name="fixtures",
                original_filename="fixtures.json",
                data=fixtures,
                sha256="unused-by-mapper",
            ),
        ),
    )
    canonical = map_snapshot(prepared)
    provider = FplForecastCsvAdapter(
        projection_path,
        canonical.players,
        season="2026-27",
        observed_at=NOW,
    )
    projections = provider.projections((GameweekNumber(value=1),)).data
    manual_request = SingleGameweekOptimisationRequest(
        target_gameweek=GameweekNumber(value=1),
        players=canonical.players,
        projections=projections,
    )
    manual = HighsSingleGameweekOptimiser().optimise(manual_request)

    manifest = build_gameweek_evidence_manifest(
        season="2026-27",
        gameweek=GameweekNumber(value=1),
        acquisition_id=UUID(int=84_900),
        snapshot_input=SnapshotEvidenceInput(
            provider_id="fpl",
            snapshot_id="synthetic-snapshot",
            observed_at=NOW,
            acquired_at=NOW,
            source_reference=str(component_root),
            bootstrap_reference=str(component_root / "bootstrap-static.json"),
            bootstrap_content=bootstrap,
            fixtures_reference=str(component_root / "fixtures.json"),
            fixtures_content=fixtures,
        ),
        projection_input=ProjectionEvidenceInput(
            provider_id="fpl_forecast",
            source="fpl-forecast",
            generated_at=NOW,
            acquired_at=NOW,
            model_version="phase9_frontend_v1",
            artifact_reference=str(projection_path),
            artifact_content=projection_path.read_bytes(),
        ),
    )
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")
    run_id = UUID(int=84_901)
    ledger = RunRecordLedger(tmp_path / "state" / "run-records")
    orchestrator = GameweekOrchestrator(
        RunRecordService(ledger, now=lambda: NOW),
        doctor=PassDoctor(),
        baseline=LocalBlankSquadBaselineRunner(state_root=tmp_path / "state"),
    )

    result = orchestrator.run(
        OrchestratorRequest(
            run_id=run_id,
            season="2026-27",
            gameweek=1,
            code_revision="commit-84",
            config_fingerprint="config-84",
            evidence_artifact=artifact,
        )
    )

    assert result.recommendation is not None
    assert result.recommendation.squad_ids == tuple(
        sorted((member.player_id for member in manual.squad.members), key=str)
    )
    assert result.recommendation.starting_xi_ids == tuple(
        sorted(manual.starting_xi, key=str)
    )
    assert result.recommendation.captain_id == manual.captain_id
    assert result.recommendation.vice_captain_id == manual.vice_captain_id
    assert result.recommendation.bench_ids == manual.bench
    assert result.recommendation.primary_objective == manual.primary_objective
    assert DuckDbDecisionRunRepository(tmp_path / "state" / "fpl.duckdb").get(run_id) is not None
