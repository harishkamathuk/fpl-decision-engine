"""Typed immutable contracts for reproducible lineup-evidence validation outputs."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel
from .lineup_evidence_evaluation import LineupEvidenceEvaluationResult
from .lineup_evidence_validation import LineupEvidenceClass, LineupEvidenceStatus
from .lineup_outcomes import (
    ChronologyReason,
    ChronologyStatus,
    JoinedLineupOutcome,
    MissingRealisedOutcome,
    OutcomeState,
    RealisedOutcome,
)

SCHEMA_VERSION = 1


class ValidationOutcomeKind(StrEnum):
    """Official outcome representation retained by a canonical validation row."""

    MISSING = "MISSING_OUTCOME"
    REALISED = "REALISED_OUTCOME"


class LineupValidationDatasetRow(DomainModel):
    """Complete replay/audit row projected from one joined #93 record."""

    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: int = Field(ge=1)
    canonical_player_id: UUID
    original_p_start: float = Field(ge=0, le=1)
    projection_provider_id: str = Field(min_length=1)
    projection_provider_version: str = Field(min_length=1)
    projection_source_reference: str | None = None
    projection_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    projection_snapshot_id: str | None = None
    projection_mapping_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    projection_model_version: str = Field(min_length=1)
    projection_generated_at: AwareDatetime
    evidence_status: LineupEvidenceStatus
    evidence_class: LineupEvidenceClass | None = None
    evidence_provider_id: str = Field(min_length=1)
    evidence_provider_version: str = Field(min_length=1)
    evidence_source_reference: str = Field(min_length=1)
    evidence_snapshot_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    evidence_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_mapping_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_published_at: AwareDatetime | None = None
    evidence_updated_at: AwareDatetime | None = None
    evidence_observed_at: AwareDatetime
    evidence_retrieved_at: AwareDatetime
    evidence_processed_at: AwareDatetime | None = None
    chronology_status: ChronologyStatus
    chronology_reasons: tuple[ChronologyReason, ...] = ()
    chronology_cutoff: AwareDatetime
    outcome_state: OutcomeState
    outcome_kind: ValidationOutcomeKind
    outcome_started: bool | None = None
    actual_minutes: int | None = Field(default=None, ge=0)
    outcome_player_ref: str | None = None
    outcome_provider_id: str | None = None
    outcome_provider_version: str | None = None
    outcome_source_reference: str | None = None
    outcome_snapshot_id: str | None = None
    outcome_retrieved_at: AwareDatetime | None = None
    outcome_finalised_at: AwareDatetime | None = None

    @property
    def logical_identity(self) -> tuple[str, int, UUID]:
        """Return the canonical dataset row identity."""

        return self.season, self.gameweek, self.canonical_player_id

    @classmethod
    def from_joined(cls, joined: JoinedLineupOutcome) -> Self:
        """Project a complete #93 record without joining or interpreting it."""

        observation = joined.observation
        evidence = observation.evidence
        outcome = joined.outcome
        realised = outcome if isinstance(outcome, RealisedOutcome) else None
        missing = outcome if isinstance(outcome, MissingRealisedOutcome) else None
        if outcome is not None and realised is None and missing is None:
            raise ValueError("joined outcome has an unsupported outcome representation")
        return cls(
            season=observation.season,
            gameweek=observation.gameweek.value,
            canonical_player_id=observation.canonical_player_id,
            original_p_start=observation.original_p_start,
            projection_provider_id=observation.projection_provider_id,
            projection_provider_version=observation.projection_provider_version,
            projection_source_reference=observation.projection_source_reference,
            projection_source_sha256=observation.projection_source_sha256,
            projection_snapshot_id=observation.projection_snapshot_id,
            projection_mapping_fingerprint=observation.projection_mapping_fingerprint,
            projection_model_version=observation.projection_model_version,
            projection_generated_at=observation.projection_generated_at,
            evidence_status=observation.evidence_status,
            evidence_class=observation.evidence_class,
            evidence_provider_id=evidence.provider_id,
            evidence_provider_version=evidence.provider_version,
            evidence_source_reference=evidence.source_reference,
            evidence_snapshot_id=evidence.snapshot_id,
            evidence_ids=evidence.evidence_ids,
            evidence_raw_sha256=evidence.raw_sha256,
            evidence_mapping_fingerprint=evidence.mapping_fingerprint,
            evidence_published_at=evidence.published_at,
            evidence_updated_at=evidence.updated_at,
            evidence_observed_at=evidence.observed_at,
            evidence_retrieved_at=evidence.retrieved_at,
            evidence_processed_at=evidence.processed_at,
            chronology_status=joined.chronology.status,
            chronology_reasons=joined.chronology.reasons,
            chronology_cutoff=joined.chronology.cutoff,
            outcome_state=joined.outcome_state,
            outcome_kind=(
                ValidationOutcomeKind.REALISED
                if realised is not None
                else ValidationOutcomeKind.MISSING
            ),
            outcome_started=realised.started if realised is not None else None,
            actual_minutes=realised.minutes if realised is not None else None,
            outcome_player_ref=(
                f"{realised.player_ref.provider}:{realised.player_ref.external_id}"
                if realised is not None
                else None
            ),
            outcome_provider_id=(
                realised.provider_id if realised is not None
                else missing.provider_id if missing else None
            ),
            outcome_provider_version=(
                realised.provider_version if realised is not None
                else missing.provider_version if missing else None
            ),
            outcome_source_reference=(
                realised.source_reference if realised is not None
                else missing.source_reference if missing else None
            ),
            outcome_snapshot_id=(
                realised.snapshot_id if realised is not None
                else missing.snapshot_id if missing else None
            ),
            outcome_retrieved_at=(
                realised.retrieved_at if realised is not None
                else missing.retrieved_at if missing else None
            ),
            outcome_finalised_at=(
                realised.finalised_at if realised is not None
                else missing.finalised_at if missing else None
            ),
        )


class LineupEvidenceValidationArtefact(DomainModel):
    """Complete immutable #95 dataset plus the exact #94 structured result."""

    schema_version: int = SCHEMA_VERSION
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    dataset: tuple[LineupValidationDatasetRow, ...]
    dataset_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation: LineupEvidenceEvaluationResult
    analysis_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluator_input_dataset_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    protocol_version: str = Field(min_length=1)
    code_version: str = Field(min_length=1)
    evidence_vocabulary_version: str = Field(min_length=1)
    official_outcome_source_hashes: tuple[tuple[str, str], ...] = ()
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artefact_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        if self.evaluation.analysis_identity != self.analysis_identity:
            raise ValueError("analysis_identity does not match embedded evaluation")
        if self.evaluation.input_dataset_identity != self.evaluator_input_dataset_identity:
            raise ValueError("evaluator input dataset identity does not match embedded evaluation")
        if self.evaluation.protocol_version != self.protocol_version:
            raise ValueError("protocol_version does not match embedded evaluation")
        if self.evaluation.code_version != self.code_version:
            raise ValueError("code_version does not match embedded evaluation")
        if self.evaluation.evidence_vocabulary_version != self.evidence_vocabulary_version:
            raise ValueError("evidence vocabulary version does not match embedded evaluation")
        if not self.dataset:
            raise ValueError("dataset must not be empty")
        if any(row.season != self.season for row in self.dataset):
            raise ValueError("dataset row season does not match artefact season")
        ordered = tuple(sorted(self.dataset, key=lambda item: item.logical_identity))
        if self.dataset != ordered:
            raise ValueError("dataset rows must use canonical ordering")
        identities = tuple(row.logical_identity for row in self.dataset)
        if len(identities) != len(set(identities)):
            raise ValueError("dataset contains duplicate logical rows")
        for name, digest in self.official_outcome_source_hashes:
            valid_digest = len(digest) == 64 and all(
                char in "0123456789abcdef" for char in digest
            )
            if not name or not valid_digest:
                raise ValueError(
                    "official outcome source hashes must contain lowercase SHA-256 digests"
                )
        if self.dataset_identity != calculate_dataset_identity(self.dataset, self.schema_version):
            raise ValueError("dataset_identity does not match dataset")
        if self.content_hash != calculate_content_hash(self):
            raise ValueError("content_hash does not match artefact payload")
        if self.artefact_identity != calculate_artefact_identity(self):
            raise ValueError("artefact_identity does not match artefact payload")
        return self


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def calculate_dataset_identity(
    dataset: tuple[LineupValidationDatasetRow, ...], schema_version: int = SCHEMA_VERSION
) -> str:
    """Calculate identity from schema version and complete canonical dataset rows."""

    payload = {
        "schema_version": schema_version,
        "dataset": [row.model_dump(mode="json") for row in dataset],
    }
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _content_payload(artefact: LineupEvidenceValidationArtefact) -> dict[str, object]:
    return {
        "schema_version": artefact.schema_version,
        "dataset": [row.model_dump(mode="json") for row in artefact.dataset],
        "dataset_identity": artefact.dataset_identity,
        "evaluation": artefact.evaluation.model_dump(mode="json"),
        "official_outcome_source_hashes": [
            {"name": name, "sha256": digest}
            for name, digest in artefact.official_outcome_source_hashes
        ],
    }


def calculate_content_hash(artefact: LineupEvidenceValidationArtefact) -> str:
    """Calculate semantic content excluding fields derived from hashes."""

    return hashlib.sha256(_canonical_json(_content_payload(artefact))).hexdigest()


def calculate_artefact_identity(artefact: LineupEvidenceValidationArtefact) -> str:
    """Calculate identity from semantic content and its content hash."""

    payload = {**_content_payload(artefact), "content_hash": artefact.content_hash}
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"
