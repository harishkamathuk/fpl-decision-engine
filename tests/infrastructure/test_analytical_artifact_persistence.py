"""Persistence tests for immutable analytical artefacts."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    ANALYTICAL_ARTIFACT_SCHEMA_V1,
    ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
    AnalyticalArtifact,
    AnalyticalArtifactRef,
    AnalyticalArtifactType,
    DecisionProvenance,
    calculate_analysis_artifact_id,
    calculate_analytical_content_hash,
)
from fpl_decision_engine.infrastructure.persistence import (
    FileAnalyticalArtifactRepository,
    parse_analytical_artifact,
    serialize_analytical_artifact,
)
from fpl_decision_engine.ports import (
    AnalyticalArtifactConflict,
    InvalidAnalyticalArtifact,
)

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
EVIDENCE_IDENTITY = f"sha256:{'e' * 64}"


def make_artifact(
    *,
    artifact_type: AnalyticalArtifactType = AnalyticalArtifactType.HISTORY,
    generator_name: str = "synthetic-analysis",
    generator_version: str = "1.0.0",
    content: dict[str, object] | None = None,
    created_at: datetime = NOW,
    schema_version: int = ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
) -> AnalyticalArtifact:
    artifact_content = content or {"points": [1, 2, 3], "summary": "stable"}
    source_run_id = UUID(int=85_001)
    source_decision_run_id = UUID(int=85_002)
    source_provenance = DecisionProvenance(
        run_id=source_run_id,
        decision_run_id=source_decision_run_id,
        evidence_identity=EVIDENCE_IDENTITY,
        decision_artifact_hash="c" * 64,
    )
    compared_decisions = (
        DecisionProvenance(
            run_id=UUID(int=85_003),
            decision_run_id=UUID(int=85_004),
            evidence_identity=f"sha256:{'d' * 64}",
            decision_artifact_hash="f" * 64,
        ),
    ) if artifact_type is AnalyticalArtifactType.COMPARISON else ()
    referenced_artifacts = (
        AnalyticalArtifactRef(
            artifact_id=f"sha256:{'1' * 64}",
            artifact_type=AnalyticalArtifactType.HISTORY,
            content_hash="2" * 64,
            source_run_id=source_run_id,
            source_decision_run_id=source_decision_run_id,
            evidence_identity=EVIDENCE_IDENTITY,
            schema_version=ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
            generator_name="history",
            generator_version="1.0.0",
        ),
    ) if artifact_type is AnalyticalArtifactType.REVIEW else ()
    v2_source = source_provenance if schema_version != ANALYTICAL_ARTIFACT_SCHEMA_V1 else None
    v2_compared = compared_decisions if v2_source is not None else ()
    v2_references = referenced_artifacts if v2_source is not None else ()
    return AnalyticalArtifact(
        analysis_artifact_id=calculate_analysis_artifact_id(
            schema_version=schema_version,
            artifact_type=artifact_type,
            source_run_id=source_run_id,
            source_decision_run_id=source_decision_run_id,
            evidence_identity=EVIDENCE_IDENTITY,
            generator_name=generator_name,
            generator_version=generator_version,
            artifact_content=artifact_content,
            source_decision_provenance=v2_source,
            compared_decisions=v2_compared,
            referenced_artifacts=v2_references,
        ),
        source_run_id=source_run_id,
        source_decision_run_id=source_decision_run_id,
        evidence_identity=EVIDENCE_IDENTITY,
        artifact_type=artifact_type,
        generator_name=generator_name,
        generator_version=generator_version,
        created_at=created_at,
        content_hash=calculate_analytical_content_hash(artifact_content),
        schema_version=schema_version,
        artifact_content=artifact_content,
        source_decision_provenance=v2_source,
        compared_decisions=v2_compared,
        referenced_artifacts=v2_references,
    )


@pytest.mark.parametrize("artifact_type", tuple(AnalyticalArtifactType))
def test_publish_round_trip_and_idempotent_regeneration(
    tmp_path: Path, artifact_type: AnalyticalArtifactType
) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    first = make_artifact(artifact_type=artifact_type)
    regenerated = make_artifact(
        artifact_type=artifact_type, created_at=NOW + timedelta(hours=1)
    )

    first_result = repository.publish(first)
    original_bytes = Path(first_result.reference).read_bytes()
    second_result = repository.publish(regenerated)

    assert second_result.analysis_artifact_id == first_result.analysis_artifact_id
    assert second_result.reference == first_result.reference
    assert Path(first_result.reference).read_bytes() == original_bytes
    assert repository.load(first.analysis_artifact_id) == first
    assert second_result.sha256 == hashlib.sha256(original_bytes).hexdigest()


def test_generator_version_and_content_regeneration_publish_new_paths(tmp_path: Path) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    original = repository.publish(make_artifact())
    new_version = repository.publish(make_artifact(generator_version="2.0.0"))
    new_content = repository.publish(make_artifact(content={"points": [9]}))

    assert (
        len(
            {
                original.analysis_artifact_id,
                new_version.analysis_artifact_id,
                new_content.analysis_artifact_id,
            }
        )
        == 3
    )
    assert len({original.reference, new_version.reference, new_content.reference}) == 3


def test_existing_conflicting_bytes_are_rejected_without_overwrite(tmp_path: Path) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    artifact = make_artifact()
    persisted = repository.publish(artifact)
    path = Path(persisted.reference)
    path.write_bytes(b'{"conflict":true}\n')
    conflicting = path.read_bytes()

    with pytest.raises(AnalyticalArtifactConflict, match="conflicting bytes"):
        repository.publish(artifact)

    assert path.read_bytes() == conflicting


def test_concurrent_conflicting_publish_never_overwrites_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    first = make_artifact(content={"winner": "first"})
    second = make_artifact(content={"winner": "second"})
    target = tmp_path / "forced-collision.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    barrier = Barrier(2)
    real_link = __import__("os").link

    def forced_path(artifact: AnalyticalArtifact) -> Path:
        return target

    def synchronised_link(source: Path, destination: Path) -> None:
        barrier.wait()
        real_link(source, destination)

    monkeypatch.setattr(repository, "_path", forced_path)
    monkeypatch.setattr(
        "fpl_decision_engine.infrastructure.persistence.analytical_artifacts.os.link",
        synchronised_link,
    )

    def publish(artifact: AnalyticalArtifact) -> object:
        try:
            return repository.publish(artifact)
        except AnalyticalArtifactConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(publish, (first, second)))

    assert sum(isinstance(result, AnalyticalArtifactConflict) for result in results) == 1
    winner = parse_analytical_artifact(target.read_bytes())
    assert winner in (first, second)
    assert target.read_bytes() == serialize_analytical_artifact(winner)


def test_parse_rejects_unsupported_schema_version() -> None:
    content = serialize_analytical_artifact(make_artifact()).replace(
        b'"schema_version":2', b'"schema_version":3'
    )

    with pytest.raises(InvalidAnalyticalArtifact, match="unsupported.*schema_version 3"):
        parse_analytical_artifact(content)


def test_historical_v1_remains_readable_immutable_and_coexists_with_v2(
    tmp_path: Path,
) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    historical = make_artifact(schema_version=ANALYTICAL_ARTIFACT_SCHEMA_V1)
    current = make_artifact()
    historical_bytes = serialize_analytical_artifact(historical)

    assert historical.analysis_artifact_id == (
        "sha256:4860d2977e7f72e27f10891864db01171ac03a036d8ce420679c423380640ba4"
    )
    assert set(json.loads(historical_bytes)) == {
        "analysis_artifact_id",
        "source_run_id",
        "source_decision_run_id",
        "evidence_identity",
        "artifact_type",
        "generator_name",
        "generator_version",
        "created_at",
        "content_hash",
        "schema_version",
        "artifact_content",
    }
    assert b'"source_decision_provenance"' not in historical_bytes
    assert b'"compared_decisions"' not in historical_bytes
    assert b'"referenced_artifacts"' not in historical_bytes
    historical_result = repository.publish(historical)
    current_result = repository.publish(current)
    before_read = Path(historical_result.reference).read_bytes()

    assert repository.load(historical.analysis_artifact_id) == historical
    assert Path(historical_result.reference).read_bytes() == before_read
    assert current.schema_version == ANALYTICAL_ARTIFACT_SCHEMA_VERSION
    assert historical_result.analysis_artifact_id != current_result.analysis_artifact_id
    assert Path(historical_result.reference).exists()
    assert Path(current_result.reference).exists()


def test_v1_payload_rejects_v2_only_provenance_fields() -> None:
    payload = json.loads(
        serialize_analytical_artifact(
            make_artifact(schema_version=ANALYTICAL_ARTIFACT_SCHEMA_V1)
        )
    )
    payload["source_decision_provenance"] = None

    with pytest.raises(InvalidAnalyticalArtifact, match="v2-only fields"):
        parse_analytical_artifact(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("payload", "content_hash does not match"),
        ("provenance", "analysis_artifact_id does not match"),
        ("content_hash", "content_hash does not match"),
        ("artifact_id", "analysis_artifact_id does not match"),
        ("reference", "analysis_artifact_id does not match"),
    ],
)
def test_load_rejects_tampered_v2_content_and_provenance(
    tmp_path: Path,
    tamper: str,
    message: str,
) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    artifact = make_artifact(artifact_type=AnalyticalArtifactType.REVIEW)
    persisted = repository.publish(artifact)
    path = Path(persisted.reference)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "payload":
        payload["artifact_content"] = {"tampered": True}
    elif tamper == "provenance":
        payload["source_decision_provenance"]["decision_artifact_hash"] = "d" * 64
    elif tamper == "content_hash":
        payload["content_hash"] = "0" * 64
    elif tamper == "artifact_id":
        payload["analysis_artifact_id"] = f"sha256:{'0' * 64}"
    else:
        payload["referenced_artifacts"][0]["artifact_id"] = f"sha256:{'9' * 64}"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidAnalyticalArtifact, match=message):
        repository.load(artifact.analysis_artifact_id)


def test_load_rejects_filename_identity_mismatch(tmp_path: Path) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    artifact = make_artifact()
    persisted = repository.publish(artifact)
    original_path = Path(persisted.reference)
    substituted_id = f"sha256:{'9' * 64}"
    substituted_path = original_path.with_name(f"{'9' * 64}.json")
    original_path.rename(substituted_path)

    with pytest.raises(InvalidAnalyticalArtifact, match="asserts.*expected"):
        repository.load(substituted_id)


def test_load_rejects_artifact_type_path_mismatch(tmp_path: Path) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    artifact = make_artifact()
    persisted = repository.publish(artifact)
    original_path = Path(persisted.reference)
    wrong_type_path = original_path.parent.parent / "review" / original_path.name
    wrong_type_path.parent.mkdir(parents=True)
    original_path.rename(wrong_type_path)

    with pytest.raises(InvalidAnalyticalArtifact, match="contradicts embedded artifact_type"):
        repository.load(artifact.analysis_artifact_id)
