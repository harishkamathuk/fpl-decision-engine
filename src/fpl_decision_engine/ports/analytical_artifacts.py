"""Ports for generating and immutably persisting derived analytical artefacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, Self, TypeVar, cast, runtime_checkable
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

from fpl_decision_engine.domain.analytical_artifact import AnalyticalArtifact
from fpl_decision_engine.domain.base import DomainModel

AnalyticalContent = dict[str, JsonValue]


class GeneratorInputData(DomainModel):
    """Canonical, deeply immutable JSON object containing preloaded generator values."""

    canonical_json: str = Field(min_length=2)

    @field_validator("canonical_json")
    @classmethod
    def value_is_canonical_json_object(cls, value: str) -> str:
        try:
            decoded: object = json.loads(value)
            if not isinstance(decoded, dict):
                raise ValueError("generator input data must contain a JSON object")
            canonical = json.dumps(
                decoded,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"generator input data must be canonical JSON: {exc}") from exc
        if value != canonical:
            raise ValueError("generator input data must use canonical JSON formatting")
        return value

    @classmethod
    def from_content(cls, content: AnalyticalContent) -> Self:
        """Freeze complete preloaded values into deterministic canonical JSON."""

        return cls(
            canonical_json=json.dumps(
                content,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def as_content(self) -> AnalyticalContent:
        """Return a fresh mutable copy for a pure generator invocation."""

        return cast(AnalyticalContent, json.loads(self.canonical_json))


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


class _AnalyticalGeneratorInput(DomainModel):
    """Immutable provenance and generator metadata shared by analytical inputs."""

    source_run_id: UUID
    evidence_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generator_name: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)


class HistoryGeneratorInput(_AnalyticalGeneratorInput):
    """Complete preloaded values used to generate one history artefact.

    history_inputs contains resolved immutable history values. Generators receive
    no repository and must not discover runs or records themselves.
    """

    source_decision_run_id: UUID | None = None
    history_inputs: GeneratorInputData


class ComparisonGeneratorInput(_AnalyticalGeneratorInput):
    """Complete preloaded values for an already-resolved comparison."""

    source_decision_run_id: UUID
    comparison_inputs: GeneratorInputData


class ReviewGeneratorInput(_AnalyticalGeneratorInput):
    """Complete preloaded decision/outcome values used to generate a review."""

    source_decision_run_id: UUID
    review_inputs: GeneratorInputData


GeneratorInputT_contra = TypeVar(
    "GeneratorInputT_contra",
    bound=_AnalyticalGeneratorInput,
    contravariant=True,
)


@runtime_checkable
class AnalyticalArtifactGenerator(Protocol[GeneratorInputT_contra]):
    """Pure generator consuming only one complete immutable input value.

    Repository loading, provenance validation and latest/previous/comparison resolution
    belong to the calling application layer.
    """

    def generate(
        self, *, generator_input: GeneratorInputT_contra
    ) -> AnalyticalContent: ...
