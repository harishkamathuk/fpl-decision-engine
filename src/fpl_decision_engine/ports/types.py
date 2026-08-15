"""Shared provider contract types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class ProviderCapability(StrEnum):
    """Capabilities that providers may advertise."""

    PLAYER_DATA = "player_data"
    FIXTURE_DATA = "fixture_data"
    GAMEWEEK_DATA = "gameweek_data"
    MANAGER_STATE = "manager_state"
    LEAGUE_STATE = "league_state"
    PROJECTIONS = "projections"
    EXPECTED_MINUTES = "expected_minutes"
    APPEARANCE_PROBABILITY = "appearance_probability"
    START_PROBABILITY = "start_probability"
    POINT_DISTRIBUTION = "point_distribution"
    XG = "xg"
    XA = "xa"
    NEWS_EVIDENCE = "news_evidence"
    OPTIMISATION = "optimisation"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Identity and capability declaration for a provider."""

    provider_id: str
    display_name: str
    version: str
    capabilities: frozenset[ProviderCapability] = frozenset()

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not self.display_name.strip():
            raise ValueError("display_name must not be blank")
        if not self.version.strip():
            raise ValueError("version must not be blank")

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ProviderProvenance:
    """Metadata required to trace a provider response back to its source.

    File-backed providers use source_sha256 for exact bytes and mapping_fingerprint
    for the deterministic external-to-canonical identity map. retrieved_at records
    local observation; payload-level generation times remain on canonical records.
    """

    provider_id: str
    provider_version: str
    retrieved_at: datetime
    source_reference: str | None = None
    snapshot_id: str | None = None
    source_sha256: str | None = None
    mapping_fingerprint: str | None = None
    season: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not self.provider_version.strip():
            raise ValueError("provider_version must not be blank")
        _require_aware(self.retrieved_at, "retrieved_at")
        for field_name, digest in (
            ("source_sha256", self.source_sha256),
            ("mapping_fingerprint", self.mapping_fingerprint),
        ):
            if digest is not None and (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class Freshness:
    """The point in time a payload represents and when it becomes stale."""

    as_of: datetime
    stale_after: timedelta | None = None

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if self.stale_after is not None and self.stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive when supplied")

    def is_stale_at(self, now: datetime) -> bool:
        _require_aware(now, "now")
        return self.stale_after is not None and now > self.as_of + self.stale_after


@dataclass(frozen=True, slots=True)
class ProviderResponse[T]:
    """Canonical envelope returned by provider ports."""

    data: T
    provenance: ProviderProvenance
    freshness: Freshness
