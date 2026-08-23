"""Persistence tests for immutable analytical artefacts."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from fpl_decision_engine.domain import (
    AnalyticalArtifact,
    AnalyticalArtifactType,
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
) -> AnalyticalArtifact:
    artifact_content = content or {"points": [1, 2, 3], "summary": "stable"}
    source_run_id = UUID(int=85_001)
    source_decision_run_id = UUID(int=85_002)
    return AnalyticalArtifact(
        analysis_artifact_id=calculate_analysis_artifact_id(
            schema_version=1,
            artifact_type=artifact_type,
            source_run_id=source_run_id,
            source_decision_run_id=source_decision_run_id,
            evidence_identity=EVIDENCE_IDENTITY,
            generator_name=generator_name,
            generator_version=generator_version,
            artifact_content=artifact_content,
        ),
        source_run_id=source_run_id,
        source_decision_run_id=source_decision_run_id,
        evidence_identity=EVIDENCE_IDENTITY,
        artifact_type=artifact_type,
        generator_name=generator_name,
        generator_version=generator_version,
        created_at=created_at,
        content_hash=calculate_analytical_content_hash(artifact_content),
        artifact_content=artifact_content,
    )


def test_publish_round_trip_and_idempotent_regeneration(tmp_path: Path) -> None:
    repository = FileAnalyticalArtifactRepository(tmp_path)
    first = make_artifact()
    regenerated = make_artifact(created_at=NOW + timedelta(hours=1))

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
        b'"schema_version":1', b'"schema_version":2'
    )

    with pytest.raises(InvalidAnalyticalArtifact, match="unsupported.*schema_version 2"):
        parse_analytical_artifact(content)
