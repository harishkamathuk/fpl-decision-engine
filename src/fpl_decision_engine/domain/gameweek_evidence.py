"""Immutable Gameweek evidence identity and manifest contracts.

The semantic identity covers what evidence means: season/Gameweek, source identities,
source observation/generation times, exact component hashes and supplied projection
lineage. Acquisition IDs, retrieval timestamps and filesystem references remain in the
manifest for audit but are deliberately excluded from the semantic digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationInfo, field_validator, model_validator

from .base import DomainModel
from .value_objects import GameweekNumber

EVIDENCE_MANIFEST_SCHEMA_VERSION = 1


def _sha256(value: str, field_name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase hexadecimal SHA-256 digest")
    return value


def _timestamp(value: datetime) -> str:
    """Canonicalise a semantic timestamp to a timezone-independent UTC representation."""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class EvidenceArtifactReference(DomainModel):
    """Immutable content digest plus a provenance-only location for reconstruction.

    ``sha256`` participates in semantic identity. ``reference`` merely locates those
    exact bytes and can change without changing the evidence identity.
    """

    reference: str = Field(min_length=1)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def digest_is_sha256(cls, value: str, info: ValidationInfo) -> str:
        return _sha256(value, info.field_name or "sha256")


class ProjectionUpstreamLineage(DomainModel):
    """One actually supplied upstream model/run lineage identifier."""

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class SnapshotEvidence(DomainModel):
    """Exact raw snapshot components and their source observation semantics.

    The source and component references are provenance-only paths. The provider,
    snapshot identity, source/as-of ``observed_at`` and exact byte hashes are semantic
    inputs; observation is distinct from retrieval. Exact source JSON bytes are hashed,
    so whitespace changes alter identity. ``content_sha256`` uses the existing #3
    snapshot aggregation algorithm over the named bootstrap and fixtures bytes.
    """

    provider_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    source_reference: str = Field(min_length=1)
    content_sha256: str
    bootstrap: EvidenceArtifactReference
    fixtures: EvidenceArtifactReference

    @field_validator("content_sha256")
    @classmethod
    def digest_is_sha256(cls, value: str, info: ValidationInfo) -> str:
        return _sha256(value, info.field_name or "content_sha256")


class ProjectionEvidence(DomainModel):
    """Projection artefact identity, timing and optional upstream model lineage.

    Projection identity is exactly ``sha256:<content digest>``. Re-observing the same
    bytes therefore reuses the same projection identity; retrieval time and location
    never invent a new one. ``generated_at`` is the forecast generation/effective time
    and is semantic. ``upstream_lineage`` is an unordered set of uniquely named
    lineage attributes, canonicalised by name/value before hashing; caller order is not
    semantic.
    """


    provider_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    projection_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generated_at: AwareDatetime
    model_version: str = Field(min_length=1)
    artifact: EvidenceArtifactReference
    upstream_lineage: tuple[ProjectionUpstreamLineage, ...] | None = None
    upstream_reference: str | None = Field(default=None, min_length=1)

    @field_validator("upstream_lineage")
    @classmethod
    def lineage_is_canonical(
        cls, value: tuple[ProjectionUpstreamLineage, ...] | None
    ) -> tuple[ProjectionUpstreamLineage, ...] | None:
        if value is None:
            return None
        if len({item.name for item in value}) != len(value):
            raise ValueError("projection upstream lineage names must be unique")
        return tuple(sorted(value, key=lambda item: (item.name, item.value)))

    @model_validator(mode="after")
    def identity_matches_content(self) -> Self:
        expected = f"sha256:{self.artifact.sha256}"
        if self.projection_id != expected:
            raise ValueError(
                "projection_id is inconsistent with projection content: "
                f"expected {expected}, observed {self.projection_id}"
            )
        return self


class EvidenceAcquisition(DomainModel):
    """Identity and timestamps for one acquisition event, separate from semantics.

    The UUID distinguishes acquisitions even when they occur at the same instant or
    resolve to identical semantic evidence. None of these fields enters the semantic
    evidence hash.
    """

    acquisition_id: UUID
    snapshot_acquired_at: AwareDatetime
    projection_acquired_at: AwareDatetime


def _semantic_payload(
    *,
    schema_version: int,
    season: str,
    gameweek: GameweekNumber,
    snapshot: SnapshotEvidence,
    projection: ProjectionEvidence,
) -> dict[str, object]:
    lineage = projection.upstream_lineage
    return {
        "gameweek": gameweek.value,
        "projection": {
            "content_sha256": projection.artifact.sha256,
            "generated_at": _timestamp(projection.generated_at),
            "model_version": projection.model_version,
            "projection_id": projection.projection_id,
            "provider_id": projection.provider_id,
            "source": projection.source,
            "upstream_lineage": (
                None
                if lineage is None
                else [{"name": item.name, "value": item.value} for item in lineage]
            ),
        },
        "schema_version": schema_version,
        "season": season,
        "snapshot": {
            "bootstrap_sha256": snapshot.bootstrap.sha256,
            "content_sha256": snapshot.content_sha256,
            "fixtures_sha256": snapshot.fixtures.sha256,
            "observed_at": _timestamp(snapshot.observed_at),
            "provider_id": snapshot.provider_id,
            "snapshot_id": snapshot.snapshot_id,
        },
    }


def calculate_evidence_identity(
    *,
    schema_version: int,
    season: str,
    gameweek: GameweekNumber,
    snapshot: SnapshotEvidence,
    projection: ProjectionEvidence,
) -> str:
    """Calculate the content-derived identity from explicit semantic fields only."""

    canonical = json.dumps(
        _semantic_payload(
            schema_version=schema_version,
            season=season,
            gameweek=gameweek,
            snapshot=snapshot,
            projection=projection,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class GameweekEvidenceManifest(DomainModel):
    """Versioned immutable manifest binding snapshot, fixtures and projections.

    Construction and parsing both recompute ``evidence_identity`` from the canonical
    semantic payload. A mismatched asserted identity is rejected rather than repaired.
    """

    schema_version: int = EVIDENCE_MANIFEST_SCHEMA_VERSION
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    snapshot: SnapshotEvidence
    projection: ProjectionEvidence
    acquisition: EvidenceAcquisition
    evidence_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("schema_version")
    @classmethod
    def supports_only_v1(cls, value: int) -> int:
        if value != EVIDENCE_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported Gameweek evidence manifest schema_version {value}; "
                f"supported: {EVIDENCE_MANIFEST_SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def asserted_identity_matches_semantics(self) -> Self:
        expected = self.reconstructed_evidence_identity()
        if self.evidence_identity != expected:
            raise ValueError(
                "Gameweek evidence identity does not match canonical semantic manifest: "
                f"expected {expected}, observed {self.evidence_identity}; do not repair "
                "identity around drifted evidence"
            )
        return self

    def semantic_payload(self) -> dict[str, object]:
        """Return the explicit canonical identity inputs, excluding provenance fields."""

        return _semantic_payload(
            schema_version=self.schema_version,
            season=self.season,
            gameweek=self.gameweek,
            snapshot=self.snapshot,
            projection=self.projection,
        )

    def canonical_semantic_bytes(self) -> bytes:
        """Serialise semantic identity inputs without ordering or whitespace ambiguity."""

        return json.dumps(
            self.semantic_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def reconstructed_evidence_identity(self) -> str:
        """Recompute the SHA-256 identity without trusting the asserted identity field."""

        return calculate_evidence_identity(
            schema_version=self.schema_version,
            season=self.season,
            gameweek=self.gameweek,
            snapshot=self.snapshot,
            projection=self.projection,
        )
