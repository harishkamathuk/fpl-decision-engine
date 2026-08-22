"""Build, validate and persist immutable Gameweek evidence manifests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from fpl_decision_engine.domain.gameweek_evidence import (
    EVIDENCE_MANIFEST_SCHEMA_VERSION,
    EvidenceAcquisition,
    EvidenceArtifactReference,
    GameweekEvidenceManifest,
    ProjectionEvidence,
    ProjectionUpstreamLineage,
    SnapshotEvidence,
    calculate_evidence_identity,
)
from fpl_decision_engine.domain.value_objects import GameweekNumber
from fpl_decision_engine.ports.persistence import UnsupportedSchemaVersion


class EvidenceDriftError(RuntimeError):
    """Evidence bytes, identity or component relationships differ from the manifest."""


class InvalidEvidenceManifest(RuntimeError):
    """A stored evidence manifest is malformed or violates the supported schema."""


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceInput:
    """Existing #3 snapshot metadata plus exact source bytes used by the builder."""

    provider_id: str
    snapshot_id: str
    observed_at: datetime
    acquired_at: datetime
    source_reference: str
    bootstrap_reference: str
    bootstrap_content: bytes
    fixtures_reference: str
    fixtures_content: bytes


@dataclass(frozen=True, slots=True)
class ProjectionEvidenceInput:
    """Existing local projection provenance plus exact supplied artefact bytes."""

    provider_id: str
    source: str
    generated_at: datetime
    acquired_at: datetime
    model_version: str
    artifact_reference: str
    artifact_content: bytes
    projection_id: str | None = None
    upstream_lineage: tuple[ProjectionUpstreamLineage, ...] | None = None
    upstream_reference: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceComponentBytes:
    """Exact bytes reconstructed through the references recorded by a manifest."""

    bootstrap: bytes
    fixtures: bytes
    projection: bytes


@dataclass(frozen=True, slots=True)
class GameweekEvidenceArtifact:
    """Stored manifest location and SHA-256 of its exact canonical JSON bytes."""

    reference: str
    sha256: str
    evidence_identity: str

    @property
    def path(self) -> Path:
        """Filesystem path for the local evidence persistence implementation."""
        return Path(self.reference)


    def read_bytes(self) -> bytes:
        """Resolve the exact persisted bytes through this typed local reference."""
        return self.path.read_bytes()

def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_content_sha256(*, bootstrap: bytes, fixtures: bytes) -> str:
    """Reproduce #3's named-resource aggregate hash over exact raw bytes.

    Resource names are included so exchanging two byte streams cannot preserve the
    aggregate. Sorting makes caller/directory order irrelevant.
    """

    digest = hashlib.sha256()
    for resource_name, data in sorted(
        (("bootstrap-static", bootstrap), ("fixtures", fixtures)), key=lambda item: item[0]
    ):
        digest.update(resource_name.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def build_gameweek_evidence_manifest(
    *,
    season: str,
    gameweek: GameweekNumber,
    acquisition_id: UUID,
    snapshot_input: SnapshotEvidenceInput,
    projection_input: ProjectionEvidenceInput,
) -> GameweekEvidenceManifest:
    """Bind exact snapshot/fixtures/projection bytes into one semantic identity.

    References and acquisition metadata remain available for audit but are omitted from
    the identity calculation. A caller-supplied projection identity is accepted only
    when it is the established content identity for the supplied bytes.
    """

    bootstrap_sha256 = _digest(snapshot_input.bootstrap_content)
    fixtures_sha256 = _digest(snapshot_input.fixtures_content)
    projection_sha256 = _digest(projection_input.artifact_content)
    expected_projection_id = f"sha256:{projection_sha256}"
    if (
        projection_input.projection_id is not None
        and projection_input.projection_id != expected_projection_id
    ):
        raise EvidenceDriftError(
            "projection identity is inconsistent with supplied content: "
            f"expected {expected_projection_id}, observed {projection_input.projection_id}; "
            f"acquisition {acquisition_id}"
        )
    snapshot = SnapshotEvidence(
        provider_id=snapshot_input.provider_id,
        snapshot_id=snapshot_input.snapshot_id,
        observed_at=snapshot_input.observed_at,
        source_reference=snapshot_input.source_reference,
        content_sha256=snapshot_content_sha256(
            bootstrap=snapshot_input.bootstrap_content,
            fixtures=snapshot_input.fixtures_content,
        ),
        bootstrap=EvidenceArtifactReference(
            reference=snapshot_input.bootstrap_reference,
            sha256=bootstrap_sha256,
        ),
        fixtures=EvidenceArtifactReference(
            reference=snapshot_input.fixtures_reference,
            sha256=fixtures_sha256,
        ),
    )
    projection = ProjectionEvidence(
        provider_id=projection_input.provider_id,
        source=projection_input.source,
        projection_id=expected_projection_id,
        generated_at=projection_input.generated_at,
        model_version=projection_input.model_version,
        artifact=EvidenceArtifactReference(
            reference=projection_input.artifact_reference,
            sha256=projection_sha256,
        ),
        upstream_lineage=projection_input.upstream_lineage,
        upstream_reference=projection_input.upstream_reference,
    )
    identity = calculate_evidence_identity(
        schema_version=EVIDENCE_MANIFEST_SCHEMA_VERSION,
        season=season,
        gameweek=gameweek,
        snapshot=snapshot,
        projection=projection,
    )
    return GameweekEvidenceManifest(
        season=season,
        gameweek=gameweek,
        snapshot=snapshot,
        projection=projection,
        acquisition=EvidenceAcquisition(
            acquisition_id=acquisition_id,
            snapshot_acquired_at=snapshot_input.acquired_at,
            projection_acquired_at=projection_input.acquired_at,
        ),
        evidence_identity=identity,
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def serialize_gameweek_evidence_manifest(manifest: GameweekEvidenceManifest) -> bytes:
    """Return deterministic canonical JSON for the full semantic and provenance record."""

    lineage = manifest.projection.upstream_lineage
    payload = {
        "acquisition": {
            "acquisition_id": str(manifest.acquisition.acquisition_id),
            "projection_acquired_at": _timestamp(manifest.acquisition.projection_acquired_at),
            "snapshot_acquired_at": _timestamp(manifest.acquisition.snapshot_acquired_at),
        },
        "evidence_identity": manifest.evidence_identity,
        "gameweek": {"value": manifest.gameweek.value},
        "projection": {
            "artifact": {
                "reference": manifest.projection.artifact.reference,
                "sha256": manifest.projection.artifact.sha256,
            },
            "generated_at": _timestamp(manifest.projection.generated_at),
            "model_version": manifest.projection.model_version,
            "projection_id": manifest.projection.projection_id,
            "provider_id": manifest.projection.provider_id,
            "source": manifest.projection.source,
            "upstream_lineage": (
                None
                if lineage is None
                else [{"name": item.name, "value": item.value} for item in lineage]
            ),
            "upstream_reference": manifest.projection.upstream_reference,
        },
        "schema_version": manifest.schema_version,
        "season": manifest.season,
        "snapshot": {
            "bootstrap": {
                "reference": manifest.snapshot.bootstrap.reference,
                "sha256": manifest.snapshot.bootstrap.sha256,
            },
            "content_sha256": manifest.snapshot.content_sha256,
            "fixtures": {
                "reference": manifest.snapshot.fixtures.reference,
                "sha256": manifest.snapshot.fixtures.sha256,
            },
            "observed_at": _timestamp(manifest.snapshot.observed_at),
            "provider_id": manifest.snapshot.provider_id,
            "snapshot_id": manifest.snapshot.snapshot_id,
            "source_reference": manifest.snapshot.source_reference,
        },
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def parse_gameweek_evidence_manifest(data: bytes | str) -> GameweekEvidenceManifest:
    """Parse a strict v1 manifest; never downgrade or repair unsupported/mismatched data."""

    try:
        parsed: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidEvidenceManifest(
            f"Gameweek evidence manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise InvalidEvidenceManifest("Gameweek evidence manifest must contain a JSON object")
    manifest_data = cast(dict[str, object], parsed)
    version = manifest_data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise InvalidEvidenceManifest(
            "Gameweek evidence manifest schema_version must be an integer"
        )
    if version != EVIDENCE_MANIFEST_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"unsupported Gameweek evidence manifest schema_version {version}; reader supports "
            f"{EVIDENCE_MANIFEST_SCHEMA_VERSION}"
        )
    try:
        return GameweekEvidenceManifest.model_validate(manifest_data)
    except ValidationError as exc:
        raise InvalidEvidenceManifest(f"invalid Gameweek evidence manifest: {exc}") from exc


def _require_component_hash(
    *,
    component: str,
    expected: str,
    content: bytes,
    manifest: GameweekEvidenceManifest,
) -> None:
    observed = _digest(content)
    if observed != expected:
        raise EvidenceDriftError(
            f"{component} content drift for evidence {manifest.evidence_identity}, "
            f"acquisition {manifest.acquisition.acquisition_id}: expected SHA-256 {expected}, "
            f"observed {observed}"
        )


def validate_gameweek_evidence(
    manifest: GameweekEvidenceManifest,
    components: EvidenceComponentBytes,
    *,
    claimed_evidence_identity: str | None = None,
) -> None:
    """Reject mutated/mixed component bytes or a downstream identity mismatch."""

    _require_component_hash(
        component="snapshot bootstrap",
        expected=manifest.snapshot.bootstrap.sha256,
        content=components.bootstrap,
        manifest=manifest,
    )
    _require_component_hash(
        component="fixtures",
        expected=manifest.snapshot.fixtures.sha256,
        content=components.fixtures,
        manifest=manifest,
    )
    observed_snapshot = snapshot_content_sha256(
        bootstrap=components.bootstrap, fixtures=components.fixtures
    )
    if observed_snapshot != manifest.snapshot.content_sha256:
        raise EvidenceDriftError(
            f"snapshot aggregate drift for evidence {manifest.evidence_identity}, acquisition "
            f"{manifest.acquisition.acquisition_id}: expected SHA-256 "
            f"{manifest.snapshot.content_sha256}, observed {observed_snapshot}"
        )
    _require_component_hash(
        component="projection",
        expected=manifest.projection.artifact.sha256,
        content=components.projection,
        manifest=manifest,
    )
    expected_projection_id = f"sha256:{_digest(components.projection)}"
    if manifest.projection.projection_id != expected_projection_id:
        raise EvidenceDriftError(
            f"projection identity drift for evidence {manifest.evidence_identity}, acquisition "
            f"{manifest.acquisition.acquisition_id}: expected {manifest.projection.projection_id}, "
            f"reconstructed {expected_projection_id}"
        )
    reconstructed = manifest.reconstructed_evidence_identity()
    if reconstructed != manifest.evidence_identity:
        raise EvidenceDriftError(
            f"semantic manifest drift: asserted {manifest.evidence_identity}, reconstructed "
            f"{reconstructed}; acquisition {manifest.acquisition.acquisition_id}"
        )
    if claimed_evidence_identity is not None and claimed_evidence_identity != reconstructed:
        raise EvidenceDriftError(
            f"downstream evidence identity mismatch: claimed {claimed_evidence_identity}, "
            f"reconstructed {reconstructed}; acquisition {manifest.acquisition.acquisition_id}"
        )


def validate_gameweek_evidence_references(
    manifest: GameweekEvidenceManifest,
    read_bytes: Callable[[str], bytes],
    *,
    claimed_evidence_identity: str | None = None,
) -> None:
    """Audit a manifest through its references without consulting mtimes or directory order."""

    contents: dict[str, bytes] = {}
    for component, reference in (
        ("snapshot bootstrap", manifest.snapshot.bootstrap.reference),
        ("fixtures", manifest.snapshot.fixtures.reference),
        ("projection", manifest.projection.artifact.reference),
    ):
        try:
            contents[component] = read_bytes(reference)
        except (OSError, KeyError) as exc:
            raise EvidenceDriftError(
                f"cannot reconstruct {component} for evidence {manifest.evidence_identity} "
                f"from reference {reference!r}: {exc}"
            ) from exc
    validate_gameweek_evidence(
        manifest,
        EvidenceComponentBytes(
            bootstrap=contents["snapshot bootstrap"],
            fixtures=contents["fixtures"],
            projection=contents["projection"],
        ),
        claimed_evidence_identity=claimed_evidence_identity,
    )


def write_gameweek_evidence_manifest(
    manifest: GameweekEvidenceManifest,
    *,
    state_root: Path = Path("state"),
) -> GameweekEvidenceArtifact:
    """Atomically persist one acquisition manifest under its semantic evidence identity."""

    content = serialize_gameweek_evidence_manifest(manifest)
    content_sha256 = _digest(content)
    semantic_digest = manifest.evidence_identity.removeprefix("sha256:")
    directory = (
        state_root
        / "gameweek-evidence"
        / f"season={manifest.season}"
        / f"gameweek={manifest.gameweek.value}"
        / semantic_digest
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{manifest.acquisition.acquisition_id}.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{manifest.acquisition.acquisition_id}.", suffix=".tmp", dir=directory
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise RuntimeError(
                    "immutable Gameweek evidence acquisition path contains conflicting "
                    f"bytes: {path}"
                ) from None
    finally:
        temporary_path.unlink(missing_ok=True)
    return GameweekEvidenceArtifact(
        reference=str(path),
        sha256=content_sha256,
        evidence_identity=manifest.evidence_identity,
    )
