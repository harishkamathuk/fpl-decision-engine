"""Versioned immutable contracts for analytical artefacts derived from decisions."""

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
from .provenance import DecisionProvenance

ANALYTICAL_ARTIFACT_SCHEMA_V1 = 1
ANALYTICAL_ARTIFACT_SCHEMA_VERSION = 2
SUPPORTED_ANALYTICAL_ARTIFACT_SCHEMAS = (
    ANALYTICAL_ARTIFACT_SCHEMA_V1,
    ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
)


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


class AnalyticalArtifactRef(DomainModel):
    """Immutable provenance required to identify one referenced analytical artefact."""

    artifact_id: str
    artifact_type: AnalyticalArtifactType
    content_hash: str
    source_run_id: UUID
    source_decision_run_id: UUID
    evidence_identity: str
    schema_version: int
    generator_name: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)

    @field_validator("artifact_id", "evidence_identity")
    @classmethod
    def prefixed_hash_is_sha256(cls, value: str, info: ValidationInfo) -> str:
        return _sha256(value, info.field_name or "digest", prefixed=True)

    @field_validator("content_hash")
    @classmethod
    def content_hash_is_sha256(cls, value: str) -> str:
        return _sha256(value, "content_hash")

    @field_validator("schema_version")
    @classmethod
    def schema_is_supported(cls, value: int) -> int:
        if value not in SUPPORTED_ANALYTICAL_ARTIFACT_SCHEMAS:
            raise ValueError(f"unsupported referenced analytical schema_version {value}")
        return value


def calculate_analytical_content_hash(content: dict[str, JsonValue]) -> str:
    """Return the SHA-256 digest of canonical analytical content bytes."""

    return hashlib.sha256(_canonical_json_bytes(content)).hexdigest()


def _decision_sort_key(value: DecisionProvenance) -> tuple[str, ...]:
    return (
        str(value.run_id),
        str(value.decision_run_id),
        value.evidence_identity,
        value.decision_artifact_hash,
    )


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
    source_decision_provenance: DecisionProvenance | None = None,
    compared_decisions: tuple[DecisionProvenance, ...] = (),
    referenced_artifacts: tuple[AnalyticalArtifactRef, ...] = (),
) -> str:
    """Derive a v1 historical or v2 provenance-complete analytical identity."""

    payload: dict[str, object] = {
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
    if schema_version == ANALYTICAL_ARTIFACT_SCHEMA_V1:
        if source_decision_provenance is not None or compared_decisions or referenced_artifacts:
            raise ValueError("schema v1 identity cannot contain v2 provenance fields")
    elif schema_version == ANALYTICAL_ARTIFACT_SCHEMA_VERSION:
        if source_decision_provenance is None:
            raise ValueError("schema v2 identity requires source decision provenance")
        payload.update(
            {
                "source_decision_provenance": source_decision_provenance.model_dump(mode="json"),
                "compared_decisions": [
                    item.model_dump(mode="json")
                    for item in sorted(compared_decisions, key=_decision_sort_key)
                ],
                "referenced_artifacts": [
                    item.model_dump(mode="json")
                    for item in sorted(referenced_artifacts, key=lambda item: item.artifact_id)
                ],
            }
        )
    else:
        raise ValueError(f"unsupported analytical artefact schema_version {schema_version}")
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()}"


class AnalyticalArtifact(DomainModel):
    """One immutable v1 or provenance-complete v2 analytical artefact."""

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
    source_decision_provenance: DecisionProvenance | None = None
    compared_decisions: tuple[DecisionProvenance, ...] = ()
    referenced_artifacts: tuple[AnalyticalArtifactRef, ...] = ()

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
    def supports_known_versions(cls, value: int) -> int:
        if value not in SUPPORTED_ANALYTICAL_ARTIFACT_SCHEMAS:
            raise ValueError(f"unsupported analytical artefact schema_version {value}")
        return value

    @model_validator(mode="after")
    def provenance_and_hashes_match_semantics(self) -> Self:
        if self.schema_version == ANALYTICAL_ARTIFACT_SCHEMA_V1:
            if (
                self.source_decision_provenance is not None
                or self.compared_decisions
                or self.referenced_artifacts
            ):
                raise ValueError("schema v1 cannot contain v2 provenance fields")
        else:
            source = self.source_decision_provenance
            if source is None:
                raise ValueError("schema v2 requires source decision provenance")
            if (
                source.run_id != self.source_run_id
                or source.decision_run_id != self.source_decision_run_id
                or source.evidence_identity != self.evidence_identity
            ):
                raise ValueError("source decision provenance contradicts artefact provenance")
            if self.artifact_type is AnalyticalArtifactType.COMPARISON:
                if not self.compared_decisions:
                    raise ValueError("comparison requires compared decision provenance")
                if self.referenced_artifacts:
                    raise ValueError("comparison cannot contain review artefact references")
            elif self.artifact_type is AnalyticalArtifactType.REVIEW:
                if not self.referenced_artifacts:
                    raise ValueError("review requires referenced analytical artefacts")
                if self.compared_decisions:
                    raise ValueError("review cannot contain compared decision provenance")
            elif self.compared_decisions or self.referenced_artifacts:
                raise ValueError("history cannot contain comparison or review provenance")
            if self.compared_decisions != tuple(
                sorted(self.compared_decisions, key=_decision_sort_key)
            ):
                raise ValueError("compared decision provenance must use canonical ordering")
            decision_ids = tuple(item.decision_run_id for item in self.compared_decisions)
            if len(set(decision_ids)) != len(decision_ids):
                raise ValueError("compared decision provenance contains conflicting duplicates")
            if self.referenced_artifacts != tuple(
                sorted(self.referenced_artifacts, key=lambda item: item.artifact_id)
            ):
                raise ValueError("referenced analytical artefacts must use canonical ordering")
            reference_ids = tuple(item.artifact_id for item in self.referenced_artifacts)
            if len(set(reference_ids)) != len(reference_ids):
                raise ValueError("referenced analytical artefacts must be unique")

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
            source_decision_provenance=self.source_decision_provenance,
            compared_decisions=self.compared_decisions,
            referenced_artifacts=self.referenced_artifacts,
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
