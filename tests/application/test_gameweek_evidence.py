"""Tests for immutable Gameweek evidence identity and #81 provenance binding."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.application.gameweek_evidence import (
    EvidenceComponentBytes,
    EvidenceDriftError,
    GameweekEvidenceArtifact,
    InvalidEvidenceManifest,
    ProjectionEvidenceInput,
    SnapshotEvidenceInput,
    build_gameweek_evidence_manifest,
    load_gameweek_evidence_artifact,
    parse_gameweek_evidence_manifest,
    serialize_gameweek_evidence_manifest,
    snapshot_content_sha256,
    validate_gameweek_evidence,
    validate_gameweek_evidence_references,
    write_gameweek_evidence_manifest,
)
from fpl_decision_engine.application.run_record_service import RunRecordService
from fpl_decision_engine.domain import (
    CloseOutcome,
    EvidenceArtifactReference,
    GameweekEvidenceManifest,
    GameweekNumber,
    ProjectionEvidence,
    ProjectionUpstreamLineage,
    RecordedDecision,
    RunArtefact,
    RunRecord,
    StageAttempt,
    StageState,
)
from fpl_decision_engine.infrastructure.ingestion import prepare_snapshot
from fpl_decision_engine.infrastructure.persistence import RunRecordLedger
from fpl_decision_engine.ports import (
    InvalidRunRecord,
    InvalidRunStateTransition,
    UnsupportedSchemaVersion,
)

OBSERVED_AT = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
GENERATED_AT = datetime(2026, 8, 21, 5, 30, tzinfo=UTC)
ACQUIRED_AT = datetime(2026, 8, 22, 9, 5, tzinfo=UTC)
DEFAULT_ACQUISITION_ID = UUID(int=83_001)
BOOTSTRAP = b'{"elements":[{"id":1}],"events":[],"teams":[]}'
FIXTURES = b'[{"id":1,"event":1}]'
PROJECTION = b"player_id,gameweek,expected_points\n1,1,5.2\n"
OTHER_BOOTSTRAP = b'{"elements":[{"id":2}],"events":[],"teams":[]}'
OTHER_FIXTURES = b'[{"id":2,"event":1}]'
OTHER_PROJECTION = b"player_id,gameweek,expected_points\n1,1,6.0\n"


def make_manifest(
    season: str = "2026-27",
    gameweek: int = 1,
    observed_at: datetime = OBSERVED_AT,
    generated_at: datetime = GENERATED_AT,
    *,
    acquisition_id: UUID = DEFAULT_ACQUISITION_ID,
    snapshot_acquired_at: datetime = ACQUIRED_AT,
    projection_acquired_at: datetime = ACQUIRED_AT,
    bootstrap: bytes = BOOTSTRAP,
    fixtures: bytes = FIXTURES,
    projection: bytes = PROJECTION,
    snapshot_id: str = "snapshot-a",
    snapshot_source_reference: str = "data/raw/a/manifest.json",
    bootstrap_reference: str = "data/raw/a/bootstrap-static.json",
    fixtures_reference: str = "data/raw/a/fixtures.json",
    projection_reference: str = "fpl-inputs/gw01/projections.csv",
    upstream_lineage: tuple[ProjectionUpstreamLineage, ...] | None = (
        ProjectionUpstreamLineage(name="model_run", value="run-7"),
        ProjectionUpstreamLineage(name="source_commit", value="abc123"),
    ),
) -> GameweekEvidenceManifest:
    return build_gameweek_evidence_manifest(
        season=season,
        gameweek=GameweekNumber(value=gameweek),
        acquisition_id=acquisition_id,
        snapshot_input=SnapshotEvidenceInput(
            provider_id="fpl",
            snapshot_id=snapshot_id,
            observed_at=observed_at,
            acquired_at=snapshot_acquired_at,
            source_reference=snapshot_source_reference,
            bootstrap_reference=bootstrap_reference,
            bootstrap_content=bootstrap,
            fixtures_reference=fixtures_reference,
            fixtures_content=fixtures,
        ),
        projection_input=ProjectionEvidenceInput(
            provider_id="fpl_forecast",
            source="fpl_forecast",
            generated_at=generated_at,
            acquired_at=projection_acquired_at,
            model_version="phase9_frontend_v1|model=baseline",
            artifact_reference=projection_reference,
            artifact_content=projection,
            upstream_lineage=upstream_lineage,
            upstream_reference="github:daniel-mehta/fpl-forecast@abc123",
        ),
    )


def components(
    *,
    bootstrap: bytes = BOOTSTRAP,
    fixtures: bytes = FIXTURES,
    projection: bytes = PROJECTION,
) -> EvidenceComponentBytes:
    return EvidenceComponentBytes(
        bootstrap=bootstrap,
        fixtures=fixtures,
        projection=projection,
    )


def test_same_semantic_evidence_has_same_identity_despite_provenance_changes() -> None:
    first = make_manifest()
    second = make_manifest(
        acquisition_id=UUID(int=83_002),
        snapshot_acquired_at=ACQUIRED_AT + timedelta(hours=1),
        projection_acquired_at=ACQUIRED_AT + timedelta(hours=2),
        snapshot_source_reference="/different/root/manifest.json",
        bootstrap_reference="/different/root/bootstrap-static.json",
        fixtures_reference="/different/root/fixtures.json",
        projection_reference="/different/root/projections.csv",
    )

    assert first.evidence_identity == second.evidence_identity
    assert first.acquisition.acquisition_id != second.acquisition.acquisition_id
    assert first.acquisition != second.acquisition


@pytest.mark.parametrize(
    ("change", "expected_difference"),
    [
        ({"bootstrap": OTHER_BOOTSTRAP}, "snapshot"),
        ({"fixtures": OTHER_FIXTURES}, "fixtures"),
        ({"projection": OTHER_PROJECTION}, "projection"),
    ],
)
def test_each_semantic_component_change_changes_identity(
    change: dict[str, bytes], expected_difference: str
) -> None:
    original = make_manifest()
    changed = make_manifest(**change)

    assert changed.evidence_identity != original.evidence_identity, expected_difference


def test_manifest_dictionary_order_and_json_formatting_do_not_change_identity() -> None:
    manifest = make_manifest()
    payload = json.loads(serialize_gameweek_evidence_manifest(manifest))
    reordered = dict(reversed(tuple(payload.items())))
    compact = json.dumps(reordered, separators=(",", ":"))
    pretty = json.dumps(payload, indent=4)

    assert parse_gameweek_evidence_manifest(compact).evidence_identity == manifest.evidence_identity
    assert parse_gameweek_evidence_manifest(pretty).evidence_identity == manifest.evidence_identity


def test_timezone_formatting_is_canonical_in_semantic_identity() -> None:
    manifest = make_manifest()
    payload = manifest.model_dump(mode="python")
    payload["snapshot"]["observed_at"] = OBSERVED_AT.astimezone(timezone(timedelta(hours=1)))
    payload["projection"]["generated_at"] = GENERATED_AT.astimezone(timezone(timedelta(hours=-4)))

    equivalent = GameweekEvidenceManifest.model_validate(payload)

    assert equivalent.evidence_identity == manifest.evidence_identity


def test_semantic_source_and_forecast_times_change_identity() -> None:
    original = make_manifest()

    assert make_manifest(observed_at=OBSERVED_AT + timedelta(seconds=1)).evidence_identity != (
        original.evidence_identity
    )
    assert make_manifest(generated_at=GENERATED_AT + timedelta(seconds=1)).evidence_identity != (
        original.evidence_identity
    )


@pytest.mark.parametrize(
    ("component", "change"),
    [
        ("bootstrap", {"bootstrap": BOOTSTRAP + b"\n"}),
        ("fixtures", {"fixtures": FIXTURES + b"\n"}),
        ("projection", {"projection": PROJECTION + b"\n"}),
    ],
)
def test_source_artifact_whitespace_changes_exact_byte_hash_and_identity(
    component: str,
    change: dict[str, bytes],
) -> None:
    original = make_manifest()
    whitespace_changed = make_manifest(**change)
    original_hashes = {
        "bootstrap": original.snapshot.bootstrap.sha256,
        "fixtures": original.snapshot.fixtures.sha256,
        "projection": original.projection.artifact.sha256,
    }
    changed_hashes = {
        "bootstrap": whitespace_changed.snapshot.bootstrap.sha256,
        "fixtures": whitespace_changed.snapshot.fixtures.sha256,
        "projection": whitespace_changed.projection.artifact.sha256,
    }

    assert changed_hashes[component] != original_hashes[component]
    assert whitespace_changed.evidence_identity != original.evidence_identity


def test_upstream_lineage_is_an_unordered_canonical_set() -> None:
    lineage = (
        ProjectionUpstreamLineage(name="model_run", value="run-7"),
        ProjectionUpstreamLineage(name="source_commit", value="abc123"),
    )

    forward = make_manifest(upstream_lineage=lineage)
    reversed_input = make_manifest(upstream_lineage=tuple(reversed(lineage)))

    assert forward.projection.upstream_lineage == reversed_input.projection.upstream_lineage
    assert forward.evidence_identity == reversed_input.evidence_identity


def test_upstream_lineage_rejects_duplicate_names() -> None:
    duplicated = (
        ProjectionUpstreamLineage(name="model_run", value="run-7"),
        ProjectionUpstreamLineage(name="model_run", value="run-8"),
    )

    with pytest.raises(ValidationError, match="lineage names must be unique"):
        make_manifest(upstream_lineage=duplicated)


def test_distinct_acquisitions_at_duplicate_timing_do_not_collide() -> None:
    first = make_manifest(acquisition_id=UUID(int=83_010))
    second = make_manifest(acquisition_id=UUID(int=83_011))

    assert first.evidence_identity == second.evidence_identity
    assert first.acquisition.snapshot_acquired_at == second.acquisition.snapshot_acquired_at
    assert first.acquisition.acquisition_id != second.acquisition.acquisition_id


@pytest.mark.parametrize(
    ("tampered", "message"),
    [
        (components(bootstrap=OTHER_BOOTSTRAP), "snapshot bootstrap content drift"),
        (components(fixtures=OTHER_FIXTURES), "fixtures content drift"),
        (components(projection=OTHER_PROJECTION), "projection content drift"),
    ],
)
def test_tampered_component_hash_is_rejected(
    tampered: EvidenceComponentBytes, message: str
) -> None:
    with pytest.raises(EvidenceDriftError, match=message):
        validate_gameweek_evidence(make_manifest(), tampered)


def test_projection_identity_inconsistent_with_content_is_rejected() -> None:
    artifact = EvidenceArtifactReference(reference="projection.csv", sha256="a" * 64)

    with pytest.raises(ValidationError, match="projection_id is inconsistent"):
        ProjectionEvidence(
            provider_id="provider",
            source="source",
            projection_id=f"sha256:{'b' * 64}",
            generated_at=GENERATED_AT,
            model_version="v1",
            artifact=artifact,
        )

    with pytest.raises(EvidenceDriftError, match="projection identity is inconsistent"):
        build_gameweek_evidence_manifest(
            season="2026-27",
            gameweek=GameweekNumber(value=1),
            acquisition_id=UUID(int=83_020),
            snapshot_input=SnapshotEvidenceInput(
                provider_id="fpl",
                snapshot_id="snapshot-a",
                observed_at=OBSERVED_AT,
                acquired_at=ACQUIRED_AT,
                source_reference="manifest.json",
                bootstrap_reference="bootstrap.json",
                bootstrap_content=BOOTSTRAP,
                fixtures_reference="fixtures.json",
                fixtures_content=FIXTURES,
            ),
            projection_input=ProjectionEvidenceInput(
                provider_id="provider",
                source="source",
                generated_at=GENERATED_AT,
                acquired_at=ACQUIRED_AT,
                model_version="v1",
                artifact_reference="projection.csv",
                artifact_content=PROJECTION,
                projection_id=f"sha256:{'b' * 64}",
            ),
        )


def test_mixing_components_from_different_evidence_states_is_rejected() -> None:
    asserted = make_manifest()
    another_state = components(
        bootstrap=OTHER_BOOTSTRAP,
        fixtures=OTHER_FIXTURES,
        projection=OTHER_PROJECTION,
    )

    with pytest.raises(EvidenceDriftError, match="expected SHA-256"):
        validate_gameweek_evidence(asserted, another_state)


def test_downstream_claimed_identity_must_match_reconstructed_manifest() -> None:
    manifest = make_manifest()

    with pytest.raises(EvidenceDriftError, match="downstream evidence identity mismatch"):
        validate_gameweek_evidence(
            manifest,
            components(),
            claimed_evidence_identity=f"sha256:{'f' * 64}",
        )


def test_unchanged_projection_can_be_reused_across_acquisitions_and_snapshots() -> None:
    first = make_manifest(acquisition_id=UUID(int=83_030))
    reacquired = make_manifest(acquisition_id=UUID(int=83_031))
    newer_snapshot = make_manifest(
        acquisition_id=UUID(int=83_032),
        bootstrap=OTHER_BOOTSTRAP,
        snapshot_id="snapshot-b",
    )

    assert first.projection.projection_id == reacquired.projection.projection_id
    assert first.projection.projection_id == newer_snapshot.projection.projection_id
    assert first.evidence_identity == reacquired.evidence_identity
    assert first.evidence_identity != newer_snapshot.evidence_identity


def test_missing_optional_upstream_lineage_remains_absent() -> None:
    manifest = make_manifest(upstream_lineage=None)
    parsed = parse_gameweek_evidence_manifest(serialize_gameweek_evidence_manifest(manifest))

    assert manifest.projection.upstream_lineage is None
    assert parsed.projection.upstream_lineage is None


def test_manifest_is_immutable_and_rejects_a_tampered_asserted_identity() -> None:
    manifest = make_manifest()

    with pytest.raises(ValidationError, match="frozen"):
        manifest.season = "2025-26"

    payload = json.loads(serialize_gameweek_evidence_manifest(manifest))
    payload["evidence_identity"] = f"sha256:{'0' * 64}"
    with pytest.raises(InvalidEvidenceManifest, match="does not match canonical"):
        parse_gameweek_evidence_manifest(json.dumps(payload))


def test_unsupported_future_schema_and_malformed_manifest_are_rejected() -> None:
    payload = json.loads(serialize_gameweek_evidence_manifest(make_manifest()))
    payload["schema_version"] = 2

    with pytest.raises(UnsupportedSchemaVersion, match="schema_version 2"):
        parse_gameweek_evidence_manifest(json.dumps(payload))
    with pytest.raises(InvalidEvidenceManifest, match="not valid JSON"):
        parse_gameweek_evidence_manifest(b"{")


def test_reference_audit_uses_exact_references_and_hashes_only() -> None:
    manifest = make_manifest(
        bootstrap_reference="z/bootstrap.json",
        fixtures_reference="a/fixtures.json",
        projection_reference="m/projection.csv",
    )
    stored = {
        "m/projection.csv": PROJECTION,
        "a/fixtures.json": FIXTURES,
        "z/bootstrap.json": BOOTSTRAP,
    }
    requested: list[str] = []

    def read_bytes(reference: str) -> bytes:
        requested.append(reference)
        return stored[reference]

    validate_gameweek_evidence_references(manifest, read_bytes)

    assert requested == ["z/bootstrap.json", "a/fixtures.json", "m/projection.csv"]


def test_snapshot_aggregate_hash_matches_existing_issue_3_algorithm() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "fpl_snapshot"
    prepared = prepare_snapshot(fixture_root)

    assert (
        snapshot_content_sha256(
            bootstrap=prepared.object_bytes("bootstrap-static"),
            fixtures=prepared.object_bytes("fixtures"),
        )
        == prepared.content_hash
    )


def test_manifest_writer_is_idempotent_and_rejects_acquisition_collision(tmp_path: Path) -> None:
    manifest = make_manifest()
    first = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")
    repeated = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")

    assert first == repeated
    assert parse_gameweek_evidence_manifest(first.path.read_bytes()) == manifest
    assert load_gameweek_evidence_artifact(first.path) == first

    conflicting = make_manifest(
        acquisition_id=manifest.acquisition.acquisition_id,
        projection_acquired_at=ACQUIRED_AT + timedelta(minutes=1),
    )
    assert conflicting.evidence_identity == manifest.evidence_identity
    with pytest.raises(RuntimeError, match="conflicting bytes"):
        write_gameweek_evidence_manifest(conflicting, state_root=tmp_path / "state")


def test_manifest_publication_is_race_safe_for_identical_and_conflicting_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    publication_number = 0

    def publish_pair(
        first: GameweekEvidenceManifest, second: GameweekEvidenceManifest
    ) -> tuple[list[object], Path]:
        barrier = Barrier(2)
        nonlocal publication_number
        publication_number += 1

        def synchronized_link(source: Path, destination: Path) -> None:
            barrier.wait()
            real_link(source, destination)

        monkeypatch.setattr(os, "link", synchronized_link)
        state_root = tmp_path / f"race-{publication_number}"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(write_gameweek_evidence_manifest, item, state_root=state_root)
                for item in (first, second)
            ]
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except RuntimeError as exc:
                outcomes.append(exc)
        target = (
            state_root
            / "gameweek-evidence"
            / f"season={first.season}"
            / f"gameweek={first.gameweek.value}"
            / first.evidence_identity.removeprefix("sha256:")
            / f"{first.acquisition.acquisition_id}.json"
        )
        return outcomes, target

    manifest = make_manifest()
    identical_outcomes, identical_target = publish_pair(manifest, manifest)
    assert all(isinstance(item, GameweekEvidenceArtifact) for item in identical_outcomes)
    assert identical_target.read_bytes() == serialize_gameweek_evidence_manifest(manifest)
    assert list(identical_target.parent.glob(".*.tmp")) == []

    conflicting = make_manifest(projection_acquired_at=ACQUIRED_AT + timedelta(minutes=1))
    conflict_outcomes, conflict_target = publish_pair(manifest, conflicting)
    assert sum(isinstance(item, GameweekEvidenceArtifact) for item in conflict_outcomes) == 1
    assert sum(isinstance(item, RuntimeError) for item in conflict_outcomes) == 1
    winner_bytes = conflict_target.read_bytes()
    assert winner_bytes in {
        serialize_gameweek_evidence_manifest(manifest),
        serialize_gameweek_evidence_manifest(conflicting),
    }
    assert conflict_target.read_bytes() == winner_bytes
    assert list(conflict_target.parent.glob(".*.tmp")) == []


def make_run(
    tmp_path: Path,
    *,
    run_id: UUID,
    season: str = "2026-27",
    gameweek: int = 1,
) -> tuple[RunRecordService, RunRecord]:
    ledger = RunRecordLedger(tmp_path / str(run_id) / "run-records")
    service = RunRecordService(ledger, now=lambda: ACQUIRED_AT)
    run = service.create_run(
        run_id=run_id,
        season=season,
        gameweek=gameweek,
        mandatory_stages=("ingest",),
    )
    return service, run


def test_create_run_emits_unbound_schema_v2(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "new-v2" / "run-records")
    service = RunRecordService(ledger, now=lambda: ACQUIRED_AT)
    run = service.create_run(
        run_id=UUID(int=83_090),
        season="2026-27",
        gameweek=1,
        mandatory_stages=("ingest",),
    )
    payload = json.loads(ledger.get_raw(run.run_id) or "{}")

    assert run.schema_version == 2
    assert run.evidence_identity is None
    assert not any(
        artefact.kind == "gameweek-evidence-manifest-v1" for artefact in run.artefacts
    )
    assert payload["schema_version"] == 2
    assert payload["evidence_identity"] is None


def test_unrelated_v1_mutation_does_not_upgrade_schema(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "v1-unrelated" / "run-records")
    historical = RunRecord(
        schema_version=1,
        run_id=UUID(int=83_091),
        season="2026-27",
        gameweek=1,
        created_at=OBSERVED_AT,
        mandatory_stages=("ingest",),
    )
    ledger.save(historical)
    service = RunRecordService(ledger, now=lambda: ACQUIRED_AT)

    updated = service.record_decision(
        historical.run_id,
        reference="state/decision.json",
        sha256="d" * 64,
        by="operator",
    )
    payload = json.loads(ledger.get_raw(historical.run_id) or "{}")

    assert updated.schema_version == 1
    assert updated.evidence_identity is None
    assert "evidence_identity" not in payload


def test_v1_to_bound_v2_preserves_all_admissible_historical_fields(tmp_path: Path) -> None:
    ledger = RunRecordLedger(tmp_path / "v1-upgrade" / "run-records")
    existing_artefact = RunArtefact(
        name="diagnostic",
        reference="state/diagnostic.json",
        sha256="a" * 64,
        kind="diagnostic",
        recorded_at=OBSERVED_AT,
    )
    decision = RecordedDecision(
        reference="state/decision.json",
        sha256="b" * 64,
        recorded_at=OBSERVED_AT,
        by="operator",
        summary="pre-binding decision",
    )
    attempt = StageAttempt(
        stage="ingest",
        attempt=1,
        status=StageState.PASS,
        started_at=OBSERVED_AT,
        finished_at=ACQUIRED_AT,
        note="historical attempt",
        by="operator",
    )
    historical = RunRecord(
        schema_version=1,
        run_id=UUID(int=83_092),
        season="2026-27",
        gameweek=1,
        created_at=OBSERVED_AT,
        previous_run_id=UUID(int=83_089),
        mandatory_stages=("ingest", "optimise"),
        stage_attempts=(attempt,),
        artefacts=(existing_artefact,),
        decisions=(decision,),
        authority_events=(),
        closed_at=None,
        code_revision="abc123",
        config_fingerprint="config-7",
        diagnostic_summary="historical diagnostic",
    )
    ledger.save(historical)
    service = RunRecordService(ledger, now=lambda: ACQUIRED_AT)
    manifest = make_manifest()
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")

    bound = service.record_evidence_manifest(
        historical.run_id,
        evidence_identity=manifest.evidence_identity,
        artifact=artifact,
    )

    assert bound.schema_version == 2
    assert bound.evidence_identity == manifest.evidence_identity
    assert bound.run_id == historical.run_id
    assert bound.season == historical.season
    assert bound.gameweek == historical.gameweek
    assert bound.created_at == historical.created_at
    assert bound.previous_run_id == historical.previous_run_id
    assert bound.mandatory_stages == historical.mandatory_stages
    assert bound.state == historical.state
    assert bound.stage_attempts == historical.stage_attempts
    assert bound.artefacts[:-1] == historical.artefacts
    assert bound.decisions == historical.decisions
    assert bound.authority_events == historical.authority_events
    assert bound.closed_at == historical.closed_at
    assert bound.code_revision == historical.code_revision
    assert bound.config_fingerprint == historical.config_fingerprint
    assert bound.diagnostic_summary == historical.diagnostic_summary
    assert bound.artefacts[-1].reference == artifact.reference
    assert bound.artefacts[-1].sha256 == artifact.sha256


def test_unbound_v2_to_bound_v2_verifies_manifest_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_100))
    manifest = make_manifest()
    assert run.schema_version == 2
    assert run.evidence_identity is None
    assert run.artefacts == ()
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")

    bound = service.record_evidence_manifest(
        run.run_id,
        evidence_identity=manifest.evidence_identity,
        artifact=artifact,
    )
    repeated = service.record_evidence_manifest(
        run.run_id,
        evidence_identity=manifest.evidence_identity,
        artifact=artifact,
    )

    assert repeated == bound
    assert bound.schema_version == 2
    assert bound.evidence_identity == manifest.evidence_identity
    assert len(bound.artefacts) == 1
    assert bound.artefacts[0].reference == artifact.reference
    assert bound.artefacts[0].sha256 == artifact.sha256


def test_run_record_binding_rejects_nonexistent_reference(tmp_path: Path) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_101))
    manifest = make_manifest()
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")
    missing = replace(artifact, reference=str(tmp_path / "missing.json"))

    with pytest.raises(InvalidRunRecord, match="cannot read persisted"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=manifest.evidence_identity,
            artifact=missing,
        )


def test_run_record_binding_rejects_wrong_reference(tmp_path: Path) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_102))
    manifest_a = make_manifest()
    manifest_b = make_manifest(bootstrap=OTHER_BOOTSTRAP, snapshot_id="snapshot-b")
    artifact_a = write_gameweek_evidence_manifest(manifest_a, state_root=tmp_path / "state")
    artifact_b = write_gameweek_evidence_manifest(manifest_b, state_root=tmp_path / "state")
    wrong_reference = replace(artifact_a, reference=artifact_b.reference)

    with pytest.raises(InvalidRunRecord, match="SHA-256 mismatch"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=manifest_a.evidence_identity,
            artifact=wrong_reference,
        )


def test_run_record_binding_rejects_forged_manifest_sha(tmp_path: Path) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_103))
    manifest = make_manifest()
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")

    with pytest.raises(InvalidRunRecord, match="SHA-256 mismatch"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=manifest.evidence_identity,
            artifact=replace(artifact, sha256="f" * 64),
        )


def test_run_record_binding_rejects_identity_a_with_manifest_b(tmp_path: Path) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_104))
    manifest_a = make_manifest()
    manifest_b = make_manifest(bootstrap=OTHER_BOOTSTRAP, snapshot_id="snapshot-b")
    artifact_b = write_gameweek_evidence_manifest(manifest_b, state_root=tmp_path / "state")

    with pytest.raises(InvalidRunRecord, match="requested evidence identity"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=manifest_a.evidence_identity,
            artifact=artifact_b,
        )


@pytest.mark.parametrize(
    ("manifest", "run_season", "run_gameweek"),
    [
        (make_manifest(season="2025-26"), "2026-27", 1),
        (make_manifest(gameweek=2), "2026-27", 1),
    ],
)
def test_run_record_binding_rejects_other_season_or_gameweek(
    tmp_path: Path,
    manifest: GameweekEvidenceManifest,
    run_season: str,
    run_gameweek: int,
) -> None:
    service, run = make_run(
        tmp_path,
        run_id=UUID(int=83_105 + manifest.gameweek.value),
        season=run_season,
        gameweek=run_gameweek,
    )
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")

    with pytest.raises(InvalidRunRecord, match="does not match run"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=manifest.evidence_identity,
            artifact=artifact,
        )


def test_run_record_rejects_another_acquisition_manifest_after_binding(tmp_path: Path) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_108))
    first = make_manifest()
    another_acquisition = make_manifest(
        acquisition_id=UUID(int=83_109),
        projection_acquired_at=ACQUIRED_AT + timedelta(minutes=1),
    )
    assert another_acquisition.evidence_identity == first.evidence_identity
    first_artifact = write_gameweek_evidence_manifest(first, state_root=tmp_path / "state")
    other_artifact = write_gameweek_evidence_manifest(
        another_acquisition, state_root=tmp_path / "state"
    )
    service.record_evidence_manifest(
        run.run_id,
        evidence_identity=first.evidence_identity,
        artifact=first_artifact,
    )

    with pytest.raises(InvalidRunRecord, match="drift requires a new run"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=first.evidence_identity,
            artifact=other_artifact,
        )


def test_run_record_rejects_evidence_binding_after_closure(tmp_path: Path) -> None:
    service, run = make_run(tmp_path, run_id=UUID(int=83_110))
    service.start_stage(run.run_id, "ingest")
    service.finish_stage(run.run_id, "ingest", StageState.PASS)
    service.close_run(run.run_id, outcome=CloseOutcome.COMPLETED)
    manifest = make_manifest()
    artifact = write_gameweek_evidence_manifest(manifest, state_root=tmp_path / "state")

    with pytest.raises(InvalidRunStateTransition, match="immutable after close"):
        service.record_evidence_manifest(
            run.run_id,
            evidence_identity=manifest.evidence_identity,
            artifact=artifact,
        )

