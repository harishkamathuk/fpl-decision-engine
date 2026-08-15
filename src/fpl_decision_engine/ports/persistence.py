"""Provider-independent contracts for canonical data and provenance persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from fpl_decision_engine.domain import DecisionRun, Fixture, Gameweek, Player, Team


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class CanonicalDatasetName(StrEnum):
    """Canonical datasets persisted at the same source-snapshot grain."""

    TEAMS = "teams"
    PLAYERS = "players"
    GAMEWEEKS = "gameweeks"
    FIXTURES = "fixtures"


@dataclass(frozen=True, slots=True)
class DatasetArtifact:
    """Immutable metadata for one curated dataset file."""

    dataset: CanonicalDatasetName
    path: str
    sha256: str
    row_count: int

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("artifact path must not be blank")
        if len(self.sha256) != 64:
            raise ValueError("artifact sha256 must be a hexadecimal SHA-256 digest")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("artifact sha256 must be a hexadecimal SHA-256 digest") from exc
        if self.row_count < 0:
            raise ValueError("artifact row_count must be non-negative")


@dataclass(frozen=True, slots=True)
class SourceObjectHash:
    """Hash of raw source evidence referenced by a curated snapshot."""

    resource_name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SnapshotRegistration:
    """Immutable catalogue record connecting curated artifacts to raw evidence."""

    provider_id: str
    season: str
    snapshot_id: str
    observed_at: datetime
    processed_at: datetime
    schema_version: int
    artifacts: tuple[DatasetArtifact, ...]
    source_hashes: tuple[SourceObjectHash, ...] = ()
    source_reference: str | None = None
    published_at: datetime | None = None
    code_revision: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.processed_at, "processed_at")
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
        if self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        datasets = [artifact.dataset for artifact in self.artifacts]
        if len(set(datasets)) != len(datasets):
            raise ValueError("snapshot registration cannot repeat a dataset artifact")


@dataclass(frozen=True, slots=True)
class CanonicalSnapshot:
    """Canonical entities and exact provenance for one immutable source snapshot."""

    provider_id: str
    season: str
    snapshot_id: str
    observed_at: datetime
    processed_at: datetime
    teams: tuple[Team, ...]
    players: tuple[Player, ...]
    gameweeks: tuple[Gameweek, ...]
    fixtures: tuple[Fixture, ...]
    source_hashes: tuple[SourceObjectHash, ...] = ()
    source_reference: str | None = None
    published_at: datetime | None = None
    code_revision: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.processed_at, "processed_at")
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")


class PersistenceError(RuntimeError):
    """Base failure raised through persistence ports."""


class ImmutableRegistrationConflict(PersistenceError):
    """An immutable catalogue identity was reused with different metadata."""


class UnsupportedSchemaVersion(PersistenceError):
    """A stored dataset uses a schema version this reader does not support."""


@runtime_checkable
class SnapshotCatalog(Protocol):
    """Catalogue immutable canonical artifacts without storing their fact rows."""

    def register(self, registration: SnapshotRegistration) -> None: ...

    def get(
        self, provider_id: str, season: str, snapshot_id: str
    ) -> SnapshotRegistration | None: ...

    def list(
        self, *, provider_id: str | None = None, season: str | None = None
    ) -> tuple[SnapshotRegistration, ...]: ...


@runtime_checkable
class CanonicalRepository(Protocol):
    """Persist and recover canonical datasets at immutable snapshot grain."""

    def save(self, snapshot: CanonicalSnapshot) -> SnapshotRegistration: ...

    def load(self, provider_id: str, season: str, snapshot_id: str) -> CanonicalSnapshot: ...


@runtime_checkable
class DecisionRunRepository(Protocol):
    """Persist reproducibility metadata for decision runs."""

    def save(self, run: DecisionRun) -> None: ...

    def get(self, run_id: UUID) -> DecisionRun | None: ...
