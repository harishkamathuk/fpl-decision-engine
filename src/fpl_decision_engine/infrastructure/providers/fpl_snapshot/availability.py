"""Offline FPL-shaped availability evidence adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from fpl_decision_engine.domain import (
    AvailabilityEvidence,
    AvailabilityReason,
    AvailabilityState,
    EvidenceAttribute,
    EvidenceConfidence,
    Player,
)
from fpl_decision_engine.infrastructure.ingestion import PreparedSnapshot
from fpl_decision_engine.ports import (
    Freshness,
    ProviderCapability,
    ProviderDataError,
    ProviderDescriptor,
    ProviderMappingError,
    ProviderProvenance,
    ProviderResponse,
)

from .schemas import SourceBootstrap, SourcePlayer

PROVIDER_ID = "fpl_snapshot_availability"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _source_player_id(player: Player, namespace: str) -> int | None:
    matches = [ref.external_id for ref in player.external_refs if ref.provider == namespace]
    if not matches:
        return None
    if len(matches) > 1:
        raise ProviderMappingError(
            f"canonical player {player.id} has ambiguous {namespace} references",
            provider_id=PROVIDER_ID,
        )
    try:
        return int(matches[0])
    except ValueError as exc:
        raise ProviderMappingError(
            f"canonical player {player.id} has invalid {namespace} reference {matches[0]!r}",
            provider_id=PROVIDER_ID,
        ) from exc


def _classify(
    player: SourcePlayer,
) -> tuple[AvailabilityState, AvailabilityReason, EvidenceConfidence]:
    chance = (
        player.chance_of_playing_this_round
        if player.chance_of_playing_this_round is not None
        else player.chance_of_playing_next_round
    )
    if player.removed is True:
        return (
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.REMOVED,
            EvidenceConfidence.DEFINITIVE,
        )
    if player.can_select is False:
        return (
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.NOT_SELECTABLE,
            EvidenceConfidence.DEFINITIVE,
        )
    if player.status == "s":
        return (
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.SUSPENSION,
            EvidenceConfidence.DEFINITIVE,
        )
    if player.status == "u":
        return (
            AvailabilityState.UNAVAILABLE,
            AvailabilityReason.OTHER,
            EvidenceConfidence.DEFINITIVE,
        )
    if player.status == "i":
        confidence = EvidenceConfidence.DEFINITIVE if chance == 0 else EvidenceConfidence.INDICATIVE
        return AvailabilityState.UNAVAILABLE, AvailabilityReason.INJURY, confidence
    if player.status == "d" or (chance is not None and chance < 100):
        return (
            AvailabilityState.DOUBTFUL,
            AvailabilityReason.DOUBTFUL,
            EvidenceConfidence.AMBIGUOUS,
        )
    if player.status == "a":
        return (
            AvailabilityState.AVAILABLE,
            AvailabilityReason.AVAILABLE,
            EvidenceConfidence.INDICATIVE,
        )
    return (
        AvailabilityState.UNKNOWN,
        AvailabilityReason.UNKNOWN,
        EvidenceConfidence.AMBIGUOUS,
    )


def _attribute(name: str, value: object) -> EvidenceAttribute:
    rendered = (
        "null" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
    )
    return EvidenceAttribute(name=name, value=rendered)


class FplSnapshotAvailabilityEvidenceProvider:
    """Extract structured evidence from an already supplied immutable snapshot.

    Season-specific FPL element IDs are resolved exactly through canonical external
    references. Free-text news is retained as evidence but never parsed to infer state.
    ``news_added`` is the only source publication time; when absent it remains absent.
    """

    def __init__(
        self,
        snapshot: PreparedSnapshot,
        players: Sequence[Player],
        *,
        processed_at: datetime,
    ) -> None:
        _require_aware(snapshot.observed_at, "snapshot.observed_at")
        _require_aware(processed_at, "processed_at")
        if processed_at < snapshot.observed_at:
            raise ValueError("processed_at cannot precede snapshot observation")
        try:
            value = json.loads(snapshot.object_bytes("bootstrap-static"))
            bootstrap = SourceBootstrap.model_validate(value)
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderDataError(
                f"unsupported FPL-shaped availability source: {exc}",
                provider_id=PROVIDER_ID,
            ) from exc

        canonical_by_external: dict[int, Player] = {}
        for player in players:
            external_id = _source_player_id(player, snapshot.provider_id)
            if external_id is None:
                continue
            if external_id in canonical_by_external:
                raise ProviderMappingError(
                    f"ambiguous canonical mapping for FPL player {external_id}",
                    provider_id=PROVIDER_ID,
                )
            canonical_by_external[external_id] = player

        source_ids = {player.id for player in bootstrap.elements}
        missing = sorted(source_ids - canonical_by_external.keys())
        if missing:
            raise ProviderMappingError(
                f"FPL availability source player {missing[0]} is not mapped to a canonical player",
                provider_id=PROVIDER_ID,
            )

        mapping_lines = [
            f"{external_id}:{canonical_by_external[external_id].id}"
            for external_id in sorted(source_ids)
        ]
        self._mapping_fingerprint = hashlib.sha256("\n".join(mapping_lines).encode()).hexdigest()
        self._snapshot = snapshot
        self._processed_at = processed_at
        self._source_players = tuple(sorted(bootstrap.elements, key=lambda item: item.id))
        self._canonical_by_external = canonical_by_external
        self._descriptor = ProviderDescriptor(
            provider_id=PROVIDER_ID,
            display_name="FPL snapshot availability evidence",
            version="1",
            capabilities=frozenset({ProviderCapability.NEWS_EVIDENCE}),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def evidence(
        self,
        player_ids: Sequence[UUID] | None = None,
    ) -> ProviderResponse[tuple[AvailabilityEvidence, ...]]:
        requested = None if player_ids is None else set(player_ids)
        if player_ids is not None and len(set(player_ids)) != len(player_ids):
            raise ProviderMappingError(
                "requested player identities must be unique", provider_id=PROVIDER_ID
            )
        known = {player.id for player in self._canonical_by_external.values()}
        if requested is not None and requested - known:
            unknown = min(requested - known, key=str)
            raise ProviderMappingError(
                f"unknown canonical player identity {unknown}", provider_id=PROVIDER_ID
            )

        records: list[AvailabilityEvidence] = []
        for source in self._source_players:
            canonical = self._canonical_by_external[source.id]
            if requested is not None and canonical.id not in requested:
                continue
            state, reason, confidence = _classify(source)
            chance = (
                source.chance_of_playing_this_round
                if source.chance_of_playing_this_round is not None
                else source.chance_of_playing_next_round
            )
            records.append(
                AvailabilityEvidence(
                    evidence_id=f"{self._snapshot.expected_snapshot_id}:player:{source.id}",
                    player_id=canonical.id,
                    state=state,
                    reason=reason,
                    confidence=confidence,
                    source_provider=self._snapshot.provider_id,
                    source_snapshot_id=self._snapshot.expected_snapshot_id,
                    source_external_player_id=str(source.id),
                    source_text=source.news or None,
                    reported_chance_percent=chance,
                    published_at=source.news_added,
                    observed_at=self._snapshot.observed_at,
                    processed_at=self._processed_at,
                    attributes=(
                        _attribute("status", source.status),
                        _attribute(
                            "chance_of_playing_this_round", source.chance_of_playing_this_round
                        ),
                        _attribute(
                            "chance_of_playing_next_round", source.chance_of_playing_next_round
                        ),
                        _attribute("can_select", source.can_select),
                        _attribute("removed", source.removed),
                    ),
                )
            )

        source_object = next(
            item for item in self._snapshot.objects if item.resource_name == "bootstrap-static"
        )
        return ProviderResponse(
            data=tuple(records),
            provenance=ProviderProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.version,
                retrieved_at=self._snapshot.observed_at,
                source_reference=(
                    f"snapshot:{self._snapshot.expected_snapshot_id}/bootstrap-static.json"
                ),
                snapshot_id=self._snapshot.expected_snapshot_id,
                source_sha256=source_object.sha256,
                mapping_fingerprint=self._mapping_fingerprint,
                season=self._snapshot.season,
            ),
            freshness=Freshness(as_of=self._snapshot.observed_at),
        )
