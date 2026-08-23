"""Immutable contracts for analytical artefacts derived from completed runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .base import DomainModel

ANALYTICAL_ARTIFACT_SCHEMA_VERSION = 1


class AnalyticalArtifactType(StrEnum):
    """Classification of a derived analytical output, not lifecycle state."""

    HISTORY = "history"
    COMPARISON = "comparison"
    REVIEW = "review"


def _sha256(value: str, field_name: str, *, prefixed: bool = False) -> str:
    digest = value.removeprefix("sha256:") if prefixed else value
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        expected = "sha256-prefixed " if prefixed else ""
        raise ValueError(f"{field_name} must be a {expected}lowercase hexadecimal SHA-256 digest")
    if prefixed and not value.startswith("sha256:"):
        raise ValueError(f"{field_name} must be prefixed with 'sha256:'")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def calculate_analytical_content_hash(content: dict[str, JsonValue]) -> str:
    """Return the SHA-256 digest of canonical analytical content bytes."""

    return hashlib.sha256(_canonical_json_bytes(content)).hexdigest()


def calculate_analysis_artifact_id(
    *,
    schema_version: int,
    artifact_type: AnalyticalArtifactType,
    source_run_id: UUID,
    source_decision_run_id: UUID | None,
    evidence_identity: str,
    generator_name: str,
    generator_version: str,
    artifact_content: dict[str, JsonValue],
) -> str:
    """Derive identity from immutable provenance, generator and analytical content.

    ``created_at`` is publication metadata and is deliberately excluded. Filesystem
    paths, mtimes, acquisition times and other mutable references are never inputs.
    """

    payload = {
        "artifact_content": artifact_content,
        "artifact_type": artifact_type.value,
        "evidence_identity": evidence_identity,
        "generator_name": generator_name,
        "generator_version": generator_version,
        "schema_version": schema_version,
        "source_decision_run_id": (
            str(source_decision_run_id) if source_decision_run_id is not None else None
        ),
        "source_run_id": str(source_run_id),
    }
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()}"


class AnalyticalArtifact(DomainModel):
    """One immutable, reproducible analysis derived from a completed decision run.

    The identity binds the classification, immutable run/evidence provenance,
    generator name/version and canonical content. ``created_at`` records first
    publication time but does not invent a new analytical identity.
    """

    analysis_artifact_id: str
    source_run_id: UUID
    source_decision_run_id: UUID | None = None
    evidence_identity: str
    artifact_type: AnalyticalArtifactType
    generator_name: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    created_at: AwareDatetime
    content_hash: str
    schema_version: int = ANALYTICAL_ARTIFACT_SCHEMA_VERSION
    artifact_content: dict[str, JsonValue]

    @field_validator("analysis_artifact_id", "evidence_identity")
    @classmethod
    def prefixed_digest_is_sha256(cls, value: str, info: ValidationInfo) -> str:
        return _sha256(value, info.field_name or "digest", prefixed=True)

    @field_validator("content_hash")
    @classmethod
    def content_digest_is_sha256(cls, value: str, info: ValidationInfo) -> str:
        return _sha256(value, info.field_name or "content_hash")

    @field_validator("schema_version")
    @classmethod
    def supports_only_v1(cls, value: int) -> int:
        if value != ANALYTICAL_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported analytical artefact schema_version {value}; supported: "
                f"{ANALYTICAL_ARTIFACT_SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def hashes_match_semantics(self) -> Self:
        expected_content_hash = calculate_analytical_content_hash(self.artifact_content)
        if self.content_hash != expected_content_hash:
            raise ValueError(
                "content_hash does not match canonical analytical content: "
                f"expected {expected_content_hash}, observed {self.content_hash}"
            )
        expected_id = calculate_analysis_artifact_id(
            schema_version=self.schema_version,
            artifact_type=self.artifact_type,
            source_run_id=self.source_run_id,
            source_decision_run_id=self.source_decision_run_id,
            evidence_identity=self.evidence_identity,
            generator_name=self.generator_name,
            generator_version=self.generator_version,
            artifact_content=self.artifact_content,
        )
        if self.analysis_artifact_id != expected_id:
            raise ValueError(
                "analysis_artifact_id does not match immutable analytical inputs: "
                f"expected {expected_id}, observed {self.analysis_artifact_id}"
            )
        return self

    @property
    def created_at_utc(self) -> str:
        """Return the publication timestamp in a stable UTC wire representation."""

        return (
            self.created_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
