"""Ports for generating and immutably persisting derived analytical artefacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from fpl_decision_engine.domain.analytical_artifact import AnalyticalArtifact
from fpl_decision_engine.domain.run_record import RunRecord

AnalyticalContent = dict[str, JsonValue]


class AnalyticalArtifactError(RuntimeError):
    """Base failure for analytical generation or persistence."""


class AnalyticalArtifactConflict(AnalyticalArtifactError):
    """An immutable analytical identity already contains conflicting bytes."""


class InvalidAnalyticalArtifact(AnalyticalArtifactError):
    """A persisted analytical artefact is malformed or semantically inconsistent."""


@dataclass(frozen=True, slots=True)
class PersistedAnalyticalArtifact:
    """Reference and exact manifest hash returned after immutable publication."""

    analysis_artifact_id: str
    reference: str
    sha256: str


@runtime_checkable
class AnalyticalArtifactRepository(Protocol):
    """Publish and load content-addressed analytical artefacts without replacement."""

    def publish(self, artifact: AnalyticalArtifact) -> PersistedAnalyticalArtifact: ...

    def load(self, analysis_artifact_id: str) -> AnalyticalArtifact | None: ...


@runtime_checkable
class HistoryArtifactGenerator(Protocol):
    """Generate deterministic history content from immutable run provenance."""

    generator_name: str
    generator_version: str

    def generate_history(self, *, source_run: RunRecord) -> AnalyticalContent: ...


@runtime_checkable
class ComparisonArtifactGenerator(Protocol):
    """Generate deterministic comparison content from immutable run provenance."""

    generator_name: str
    generator_version: str

    def generate_comparison(self, *, source_run: RunRecord) -> AnalyticalContent: ...


@runtime_checkable
class ReviewArtifactGenerator(Protocol):
    """Generate deterministic review content from immutable run provenance."""

    generator_name: str
    generator_version: str

    def generate_review(self, *, source_run: RunRecord) -> AnalyticalContent: ...
