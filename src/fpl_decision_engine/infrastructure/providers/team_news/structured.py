"""Structured team-news evidence provider."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, ValidationError, model_validator

from fpl_decision_engine.domain import (
    AvailabilityEvidence,
    AvailabilityReason,
    AvailabilityState,
    EvidenceAttribute,
    EvidenceConfidence,
    Player,
)
from fpl_decision_engine.ports import (
    Freshness,
    ProviderCapability,
    ProviderDataError,
    ProviderDescriptor,
    ProviderMappingError,
    ProviderProvenance,
    ProviderResponse,
)
from fpl_decision_engine.ports.providers import NewsEvidenceProvider

PROVIDER_ID = "structured_team_news_evidence"
PROVIDER_VERSION = "1"
SCHEMA_VERSION = 1


class _StructuredTeamNewsRecordV1(BaseModel):
    """One evidence record within a structured team-news artefact."""

    model_config = {"extra": "forbid"}

    evidence_id: str = Field(min_length=1)
    source_external_player_id: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    state: str
    reason: str
    confidence: str
    published_at: AwareDatetime | None = None
    source_text: str | None = None
    attributes: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_enum_values(self) -> _StructuredTeamNewsRecordV1:
        try:
            AvailabilityState(self.state)
        except ValueError:
            raise ValueError(f"invalid state value: {self.state!r}") from None
        try:
            AvailabilityReason(self.reason)
        except ValueError:
            raise ValueError(f"invalid reason value: {self.reason!r}") from None
        try:
            EvidenceConfidence(self.confidence)
        except ValueError:
            raise ValueError(f"invalid confidence value: {self.confidence!r}") from None
        return self

    @model_validator(mode="after")
    def validate_published_at_tzaware(self) -> _StructuredTeamNewsRecordV1:
        if self.published_at is not None and (
            self.published_at.tzinfo is None or self.published_at.utcoffset() is None
        ):
            raise ValueError("published_at must be timezone-aware")
        return self


class _StructuredTeamNewsArtifactV1(BaseModel):
    """Top-level structured team-news evidence artefact."""

    model_config = {"extra": "forbid"}

    schema_version: int = SCHEMA_VERSION
    source_provider: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    evidence: tuple[_StructuredTeamNewsRecordV1, ...] = ()

    @model_validator(mode="after")
    def validate_schema_version(self) -> _StructuredTeamNewsArtifactV1:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema version: {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        return self

    @model_validator(mode="after")
    def validate_no_duplicate_evidence_ids(self) -> _StructuredTeamNewsArtifactV1:
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence_id values")
        return self


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _canonicalise_attribute_value(value: object) -> str:
    """Deterministically render a scalar JSON value to a string for EvidenceAttribute."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError(f"non-scalar attribute value: {value!r}")


def _build_evidence_attributes(
    raw_attributes: dict[str, object] | None,
    source_reference_value: str,
) -> tuple[EvidenceAttribute, ...]:
    """Build EvidenceAttribute tuple: source_reference first, then sorted
    source-specific attributes."""
    attrs: list[EvidenceAttribute] = []

    # Always include source_reference as a retained attribute
    attrs.append(EvidenceAttribute(name="source_reference", value=source_reference_value))

    if raw_attributes is None:
        return tuple(attrs)

    # Add source-specific attributes, excluding source_reference
    names: list[str] = []
    for key in raw_attributes:
        if key == "source_reference":
            raise ValueError(
                "attributes must not define source_reference; use the reserved attribute"
            )
        try:
            _canonicalise_attribute_value(raw_attributes[key])
        except ValueError:
            raise
        names.append(key)

    if len(names) != len(set(names)):
        raise ValueError("attribute names must be unique")

    for key in sorted(names):
        value = _canonicalise_attribute_value(raw_attributes[key])
        attrs.append(EvidenceAttribute(name=key, value=value))

    return tuple(attrs)


class StructuredTeamNewsEvidenceProvider(NewsEvidenceProvider[AvailabilityEvidence]):
    """Ingest immutable external football evidence for pre-deadline review."""

    def __init__(
        self,
        path: Path,
        players: Sequence[Player],
        *,
        processed_at: datetime,
    ) -> None:
        path = Path(path)
        _require_aware(processed_at, "processed_at")

        # 1. resolve path; read exact source bytes once
        raw_bytes = path.resolve().read_bytes()

        # 2. calculate SHA-256 over exact bytes
        source_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        # 3. decode UTF-8 and parse JSON
        try:
            value = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderDataError(
                f"malformed JSON source file: {exc}",
                provider_id=PROVIDER_ID,
            ) from exc

        # 4. validate _StructuredTeamNewsArtifactV1
        try:
            artifact = _StructuredTeamNewsArtifactV1.model_validate(value)
        except ValidationError as exc:
            raise ProviderDataError(
                f"invalid structured team-news artefact: {exc}",
                provider_id=PROVIDER_ID,
            ) from exc

        # 5. require processed_at to be timezone-aware
        _require_aware(processed_at, "processed_at")

        # 6. require processed_at >= artifact.observed_at
        if processed_at < artifact.observed_at:
            raise ValueError("processed_at cannot precede artifact observed_at")

        # 7. build exact ExternalRef mapping
        #    For each player, collect external_refs matching artifact.source_provider
        #    Reject if any player has more than one ExternalRef for the source_provider
        external_to_player: dict[str, UUID] = {}
        ambiguity_external: set[str] = set()
        multi_ref_same_player: set[str] = set()

        for player in players:
            provider_refs = [
                ref for ref in player.external_refs if ref.provider == artifact.source_provider
            ]
            if len(provider_refs) > 1:
                multi_ref_same_player.update(ref.external_id for ref in provider_refs)
            for ref in provider_refs:
                ext_id = ref.external_id
                if ext_id in external_to_player:
                    existing_player_id = external_to_player[ext_id]
                    if existing_player_id != player.id:
                        ambiguity_external.add(ext_id)
                else:
                    external_to_player[ext_id] = player.id

        if multi_ref_same_player:
            ids = sorted(multi_ref_same_player)
            raise ProviderMappingError(
                f"canonical player has multiple ExternalRef entries for "
                f"{artifact.source_provider}:{ids}",
                provider_id=PROVIDER_ID,
            )

        # Now validate each evidence record's source_external_player_id
        for record in artifact.evidence:
            ext_id = record.source_external_player_id
            if ext_id not in external_to_player:
                raise ProviderMappingError(
                    f"no canonical player mapped to {artifact.source_provider}:{ext_id}",
                    provider_id=PROVIDER_ID,
                )
            if ext_id in ambiguity_external:
                raise ProviderMappingError(
                    f"ambiguous canonical mapping for {artifact.source_provider}:{ext_id}; "
                    f"multiple players match",
                    provider_id=PROVIDER_ID,
                )

        # Build sorted mapping lines for fingerprint (only for external_ids in evidence)
        evidence_external_ids = sorted({rec.source_external_player_id for rec in artifact.evidence})
        mapping_lines: list[str] = []
        for ext_id in evidence_external_ids:
            player_id = external_to_player[ext_id]
            mapping_lines.append(f"{ext_id}:{player_id}")
        self._mapping_fingerprint = hashlib.sha256("\n".join(mapping_lines).encode()).hexdigest()

        # Store all derived state
        self._resolved = path.resolve()
        self._raw_bytes = raw_bytes
        self._source_sha256 = source_sha256
        self._artifact = artifact
        self._external_to_player = external_to_player
        self._artifact_source_provider = artifact.source_provider
        self._artifact_observed_at = artifact.observed_at
        self._processed_at = processed_at
        self._players = tuple(players)

        # Pre-compute the set of all canonical player IDs that appear in this evidence
        self._canonical_player_ids = {
            self._external_to_player[rec.source_external_player_id]
            for rec in self._artifact.evidence
        }

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=PROVIDER_ID,
            display_name="Structured team-news evidence",
            version=PROVIDER_VERSION,
            capabilities=frozenset({ProviderCapability.NEWS_EVIDENCE}),
        )

    def evidence(
        self,
        player_ids: Sequence[UUID] | None = None,
    ) -> ProviderResponse[tuple[AvailabilityEvidence, ...]]:
        """Return availability evidence tuples, optionally filtered by player IDs."""
        requested = None if player_ids is None else set(player_ids)

        # Validate requested IDs: no duplicates, all must be known canonical IDs
        if player_ids is not None and requested is not None:
            if len(player_ids) != len(set(player_ids)):
                raise ProviderMappingError(
                    "requested player identities must be unique", provider_id=PROVIDER_ID
                )
            requested = set(player_ids)
            unknown = requested - self._canonical_player_ids
            if unknown:
                unknown_id = min(unknown, key=str)
                raise ProviderMappingError(
                    f"unknown canonical player identity {unknown_id}", provider_id=PROVIDER_ID
                )

        records: list[AvailabilityEvidence] = []
        for record in self._artifact.evidence:
            # Get canonical player_id for this evidence record's source_external_player_id
            ext_id = record.source_external_player_id
            player_id = self._external_to_player[ext_id]

            # Apply player filtering
            if requested is not None and player_id not in requested:
                continue

            # Build attributes: source_reference first, then sorted source-specific attributes
            attrs = _build_evidence_attributes(record.attributes, record.source_reference)

            try:
                evidence = AvailabilityEvidence(
                    evidence_id=record.evidence_id,
                    player_id=player_id,
                    state=AvailabilityState(record.state),
                    reason=AvailabilityReason(record.reason),
                    confidence=EvidenceConfidence(record.confidence),
                    source_provider=self._artifact_source_provider,
                    source_snapshot_id=self._artifact.source_snapshot_id,
                    source_external_player_id=ext_id,
                    source_text=record.source_text,
                    reported_chance_percent=None,
                    published_at=record.published_at,
                    observed_at=self._artifact_observed_at,
                    processed_at=self._processed_at,
                    attributes=attrs,
                )
            except ValidationError as exc:
                raise ProviderDataError(
                    f"invalid evidence data: {exc}",
                    provider_id=PROVIDER_ID,
                ) from exc
            records.append(evidence)

        # Sort by evidence_id for deterministic ordering
        records.sort(key=lambda r: r.evidence_id)

        # Build provenance
        provenance = ProviderProvenance(
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            retrieved_at=self._artifact_observed_at,
            source_reference=str(self._resolved),
            snapshot_id=self._artifact.source_snapshot_id,
            source_sha256=self._source_sha256,
            mapping_fingerprint=self._mapping_fingerprint,
            season=None,
        )

        freshness = Freshness(as_of=self._artifact_observed_at)

        return ProviderResponse(
            data=tuple(records),
            provenance=provenance,
            freshness=freshness,
        )
