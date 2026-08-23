"""Immutable provenance values shared by decisions and analytical artefacts."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator

from .base import DomainModel


class DecisionProvenance(DomainModel):
    """Identity of one immutable decision artefact and the evidence that produced it."""

    run_id: UUID
    decision_run_id: UUID
    evidence_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision_artifact_hash: str = Field(min_length=64, max_length=64)

    @field_validator("decision_artifact_hash")
    @classmethod
    def artifact_hash_is_lowercase_sha256(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError(
                "decision_artifact_hash must be a lowercase hexadecimal SHA-256 digest"
            )
        return value
