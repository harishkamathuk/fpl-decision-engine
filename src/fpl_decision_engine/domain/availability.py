"""Canonical availability evidence and post-forecast assessment contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel


class AvailabilityState(StrEnum):
    """Source-independent statement about a player's current availability."""

    AVAILABLE = "available"
    DOUBTFUL = "doubtful"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class AvailabilityReason(StrEnum):
    """Why a source reported an availability state."""

    AVAILABLE = "available"
    INJURY = "injury"
    SUSPENSION = "suspension"
    DOUBTFUL = "doubtful"
    NOT_SELECTABLE = "not_selectable"
    REMOVED = "removed"
    OTHER = "other"
    UNKNOWN = "unknown"


class EvidenceConfidence(StrEnum):
    """Strength of the source statement, not a numeric forecast probability."""

    DEFINITIVE = "definitive"
    INDICATIVE = "indicative"
    AMBIGUOUS = "ambiguous"


class EvidenceTemporalRelation(StrEnum):
    """Relationship between publication and the base forecast generation time."""

    NEWER = "newer"
    SAME_TIME = "same_time"
    OLDER = "older"
    UNKNOWN = "unknown"


class AvailabilityDisposition(StrEnum):
    """Conservative decision effect of assessed post-forecast evidence."""

    NO_ACTION = "no_action"
    EXCLUDE = "exclude"
    REVIEW = "review"
    CONFLICT = "conflict"


class EvidenceTiming(DomainModel):
    """Auditable relationship between one evidence item and its base forecast."""

    evidence_id: str = Field(min_length=1)
    relation: EvidenceTemporalRelation


class EvidenceAttribute(DomainModel):
    """Exact source value retained without placing provider fields on Player."""

    name: str = Field(min_length=1)
    value: str


class AvailabilityEvidence(DomainModel):
    """Immutable source observation attached to one exact canonical player.

    ``published_at`` is the source's knowledge time when supplied. ``observed_at`` is
    when the containing snapshot was observed locally, and ``processed_at`` is when the
    adapter produced this canonical record. Missing publication time remains missing;
    observation time is not silently substituted because that could make old evidence
    appear to be a post-forecast change.
    """

    evidence_id: str = Field(min_length=1)
    player_id: UUID
    state: AvailabilityState
    reason: AvailabilityReason
    confidence: EvidenceConfidence
    source_provider: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    source_external_player_id: str = Field(min_length=1)
    source_text: str | None = None
    reported_chance_percent: int | None = Field(default=None, ge=0, le=100)
    published_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    processed_at: AwareDatetime
    attributes: tuple[EvidenceAttribute, ...] = ()

    @model_validator(mode="after")
    def validate_time_and_attributes(self) -> Self:
        if self.published_at is not None and self.published_at > self.observed_at:
            raise ValueError("availability evidence cannot be observed before publication")
        if self.processed_at < self.observed_at:
            raise ValueError("availability evidence cannot be processed before observation")
        names = [attribute.name for attribute in self.attributes]
        if len(names) != len(set(names)):
            raise ValueError("availability evidence attributes must have unique names")
        return self


class AvailabilityAssessment(DomainModel):
    """Auditable disposition of all evidence for one immutable base projection."""

    player_id: UUID
    projection_generated_at: AwareDatetime
    evidence: tuple[AvailabilityEvidence, ...] = ()
    evidence_timing: tuple[EvidenceTiming, ...] = ()
    disposition: AvailabilityDisposition
    applied_evidence_ids: tuple[str, ...] = ()
    superseded_evidence_ids: tuple[str, ...] = ()
    already_known_evidence_ids: tuple[str, ...] = ()
    stale_evidence_ids: tuple[str, ...] = ()
    unknown_time_evidence_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def timing_matches_evidence(self) -> Self:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("assessment evidence IDs must be unique")
        if any(item.player_id != self.player_id for item in self.evidence):
            raise ValueError("assessment evidence must reference its assessed player")
        timing_ids = tuple(item.evidence_id for item in self.evidence_timing)
        if timing_ids != evidence_ids:
            raise ValueError("assessment timing must correspond to evidence in order")
        categories = (
            self.applied_evidence_ids,
            self.superseded_evidence_ids,
            self.already_known_evidence_ids,
            self.stale_evidence_ids,
            self.unknown_time_evidence_ids,
        )
        categorized_ids: set[str] = set()
        for category in categories:
            category_ids = set(category)
            if categorized_ids & category_ids:
                raise ValueError("assessment evidence categories must be mutually disjoint")
            categorized_ids.update(category_ids)
        if categorized_ids != set(evidence_ids):
            raise ValueError("assessment must categorize every evidence ID exactly once")
        return self

    @property
    def excluded(self) -> bool:
        return self.disposition is AvailabilityDisposition.EXCLUDE

    @property
    def requires_review(self) -> bool:
        return self.disposition in {
            AvailabilityDisposition.REVIEW,
            AvailabilityDisposition.CONFLICT,
        }


class AvailabilityAssessmentSet(DomainModel):
    """Assessments plus the only Phase-1 optimiser effects they may produce."""

    assessments: tuple[AvailabilityAssessment, ...]
    excluded_player_ids: frozenset[UUID] = frozenset()
    review_player_ids: frozenset[UUID] = frozenset()
    conflict_player_ids: frozenset[UUID] = frozenset()

    @model_validator(mode="after")
    def summary_sets_match_assessments(self) -> Self:
        player_ids = [item.player_id for item in self.assessments]
        if len(player_ids) != len(set(player_ids)):
            raise ValueError("availability assessments must have unique players")
        expected_excluded = frozenset(item.player_id for item in self.assessments if item.excluded)
        expected_review = frozenset(
            item.player_id for item in self.assessments if item.requires_review
        )
        expected_conflict = frozenset(
            item.player_id
            for item in self.assessments
            if item.disposition is AvailabilityDisposition.CONFLICT
        )
        if self.excluded_player_ids != expected_excluded:
            raise ValueError("excluded player summary does not match assessments")
        if self.review_player_ids != expected_review:
            raise ValueError("review player summary does not match assessments")
        if self.conflict_player_ids != expected_conflict:
            raise ValueError("conflict player summary does not match assessments")
        return self
