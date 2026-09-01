"""Canonical realised lineup outcomes and chronology decisions for validation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel
from .lineup_evidence_validation import LineupEvidenceValidationObservation
from .value_objects import ExternalRef, GameweekNumber


class OutcomeState(StrEnum):
    """Whether an official live-row outcome was present and what it recorded."""

    MISSING = "MISSING_OUTCOME"
    NON_START = "NON_START"
    STARTED = "STARTED"


class ChronologyStatus(StrEnum):
    """Eligibility of a prospective observation for validation."""

    VALID = "VALID"
    EXCLUDED_CHRONOLOGY = "EXCLUDED_CHRONOLOGY"
    EXCLUDED_CHRONOLOGY_UNPROVEN = "EXCLUDED_CHRONOLOGY_UNPROVEN"


class ChronologyReason(StrEnum):
    """Deterministic reasons an observation cannot enter valid analysis."""

    PROJECTION_AT_OR_AFTER_CUTOFF = "PROJECTION_AT_OR_AFTER_CUTOFF"
    EVIDENCE_PUBLISHED_AT_OR_AFTER_CUTOFF = "EVIDENCE_PUBLISHED_AT_OR_AFTER_CUTOFF"
    EVIDENCE_OBSERVED_AT_OR_AFTER_CUTOFF = "EVIDENCE_OBSERVED_AT_OR_AFTER_CUTOFF"
    PROJECTION_TIMESTAMP_MISSING = "PROJECTION_TIMESTAMP_MISSING"
    EVIDENCE_CHRONOLOGY_UNPROVEN = "EVIDENCE_CHRONOLOGY_UNPROVEN"
    TIMESTAMP_INVALID = "TIMESTAMP_INVALID"


class ChronologyInput(DomainModel):
    """Narrow #93 input allowing incomplete raw chronology to fail closed.

    ``observed_at`` is the acquisition boundary. ``processed_at`` is retained only
    as provenance and never determines deadline eligibility.
    """

    projection_generated_at: datetime | None = None
    evidence_observed_at: datetime | None = None
    evidence_published_at: datetime | None = None
    evidence_processed_at: datetime | None = None
    chronology_proven: bool = True
    timestamp_invalid: bool = False

    @classmethod
    def from_observation(cls, observation: LineupEvidenceValidationObservation) -> ChronologyInput:
        """Extract only chronology facts from a complete immutable #92 observation."""

        return cls(
            projection_generated_at=observation.projection_generated_at,
            evidence_observed_at=observation.evidence.observed_at,
            evidence_published_at=observation.evidence.published_at,
            evidence_processed_at=observation.evidence.processed_at,
        )

    @model_validator(mode="after")
    def validate_timestamp_shape(self) -> ChronologyInput:
        values = (
            self.projection_generated_at,
            self.evidence_observed_at,
            self.evidence_published_at,
            self.evidence_processed_at,
        )
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in values
        ):
            object.__setattr__(self, "timestamp_invalid", True)
        return self


class RealisedOutcome(DomainModel):
    """One official FPL live-row outcome, keyed by season-specific element ID."""

    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    player_ref: ExternalRef
    canonical_player_id: UUID
    started: bool
    minutes: int = Field(ge=0, le=130)
    source_reference: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    retrieved_at: AwareDatetime
    finalised_at: AwareDatetime

    @property
    def logical_identity(self) -> tuple[str, int, UUID]:
        return self.season, self.gameweek.value, self.canonical_player_id

    @model_validator(mode="after")
    def started_matches_official_flag(self) -> RealisedOutcome:
        if self.player_ref.provider != "fpl-element":
            raise ValueError("player_ref provider must be fpl-element")
        if not self.started and self.minutes != 0:
            raise ValueError("official non-start must have zero minutes")
        return self


class MissingRealisedOutcome(DomainModel):
    """Explicit absence of an official live row; never equivalent to non-start."""

    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: GameweekNumber
    canonical_player_id: UUID
    source_reference: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    retrieved_at: AwareDatetime
    finalised_at: AwareDatetime


class ChronologyDecision(DomainModel):
    """Auditable chronology decision retained for accepted and excluded rows."""

    status: ChronologyStatus
    reasons: tuple[ChronologyReason, ...] = ()
    cutoff: AwareDatetime

    @model_validator(mode="after")
    def reason_shape_is_valid(self) -> ChronologyDecision:
        if self.status is ChronologyStatus.VALID and self.reasons:
            raise ValueError("VALID chronology cannot contain exclusion reasons")
        if self.status is not ChronologyStatus.VALID and not self.reasons:
            raise ValueError("excluded chronology requires at least one reason")
        return self


class JoinedLineupOutcome(DomainModel):
    """Joined observation preserving chronology, outcome state and provenance."""

    observation: LineupEvidenceValidationObservation
    chronology: ChronologyDecision
    outcome: RealisedOutcome | MissingRealisedOutcome | None
    outcome_state: OutcomeState

    @model_validator(mode="after")
    def outcome_shape_is_valid(self) -> JoinedLineupOutcome:
        if self.outcome_state is OutcomeState.MISSING:
            if self.outcome is not None and not isinstance(self.outcome, MissingRealisedOutcome):
                raise ValueError("MISSING_OUTCOME requires a missing outcome record")
        elif not isinstance(self.outcome, RealisedOutcome):
            raise ValueError("realised start states require an official outcome record")
        return self

    @property
    def logical_identity(self) -> tuple[str, int, UUID]:
        return self.observation.logical_identity
