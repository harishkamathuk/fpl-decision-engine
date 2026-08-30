"""Immutable prospective observations joining forecasts with lineup evidence."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel
from .models import Projection
from .value_objects import GameweekNumber

SCHEMA_VERSION = 1


class LineupEvidenceClass(StrEnum):
    """The three approved contemporary lineup evidence classifications."""

    SUPPORTS_START = "SUPPORTS_START"
    SUPPORTS_BENCH = "SUPPORTS_BENCH"
    NO_MATERIAL_SIGNAL = "NO_MATERIAL_SIGNAL"


class LineupEvidenceStatus(StrEnum):
    """Whether evidence was classified, missing, or conflicting."""

    CLASSIFIED = "CLASSIFIED"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"


class LineupEvidenceProvenance(DomainModel):
    """Domain-owned provenance copied from an already acquired evidence result."""

    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    snapshot_id: str | None = Field(default=None, min_length=1)
    evidence_ids: tuple[str, ...] = ()
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    published_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    retrieved_at: AwareDatetime
    processed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> LineupEvidenceProvenance:
        if self.published_at is not None and self.updated_at is not None:
            if self.updated_at < self.published_at:
                raise ValueError("updated_at cannot precede published_at")
        if self.retrieved_at < self.observed_at:
            raise ValueError("retrieved_at cannot precede observed_at")
        if self.processed_at is not None and self.processed_at < self.retrieved_at:
            raise ValueError("processed_at cannot precede retrieved_at")
        return self


class LineupEvidenceValidationObservation(DomainModel):
    """One immutable prospective player/Gameweek decision-time observation."""

    schema_version: int = SCHEMA_VERSION
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    canonical_player_id: UUID
    projection_provider_id: str = Field(min_length=1)
    projection_provider_version: str = Field(min_length=1)
    projection_source_reference: str | None = None
    projection_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    projection_snapshot_id: str | None = None
    projection_mapping_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    projection_model_version: str = Field(min_length=1)
    projection_generated_at: AwareDatetime
    original_p_start: float = Field(ge=0, le=1)
    evidence_status: LineupEvidenceStatus
    evidence_class: LineupEvidenceClass | None = None
    evidence: LineupEvidenceProvenance

    @model_validator(mode="after")
    def validate_classification(self) -> LineupEvidenceValidationObservation:
        if self.evidence_status is LineupEvidenceStatus.CLASSIFIED and self.evidence_class is None:
            raise ValueError("CLASSIFIED observations require exactly one evidence class")
        if self.evidence_status is not LineupEvidenceStatus.CLASSIFIED and self.evidence_class is not None:
            raise ValueError("MISSING and CONFLICTING observations must not have an evidence class")
        return self

    @property
    def logical_identity(self) -> tuple[str, int, UUID]:
        """Return the immutable season/Gameweek/player identity."""

        return self.season, self.gameweek.value, self.canonical_player_id

    @classmethod
    def from_projection(
        cls,
        *,
        season: str,
        projection: Projection,
        projection_provider_version: str,
        projection_source_reference: str | None,
        projection_source_sha256: str | None,
        projection_snapshot_id: str | None,
        projection_mapping_fingerprint: str | None,
        evidence_status: LineupEvidenceStatus,
        evidence_class: LineupEvidenceClass | None,
        evidence: LineupEvidenceProvenance,
    ) -> LineupEvidenceValidationObservation:
        """Preserve the supplied projection without interpretation or adjustment."""

        if projection.start_probability is None:
            raise ValueError("projection must contain start_probability")
        if evidence.provider_id.strip() == "":
            raise ValueError("evidence provider_id must not be blank")
        return cls(
            season=season,
            gameweek=projection.gameweek,
            canonical_player_id=projection.player_id,
            projection_provider_id=projection.source,
            projection_provider_version=projection_provider_version,
            projection_source_reference=projection_source_reference,
            projection_source_sha256=projection_source_sha256,
            projection_snapshot_id=projection_snapshot_id,
            projection_mapping_fingerprint=projection_mapping_fingerprint,
            projection_model_version=projection.model_version,
            projection_generated_at=projection.generated_at,
            original_p_start=projection.start_probability,
            evidence_status=evidence_status,
            evidence_class=evidence_class,
            evidence=evidence,
        )

    def original_projection_probability(self) -> float:
        """Return the frozen provider probability; no revised Projection is emitted."""

        return self.original_p_start
EOF