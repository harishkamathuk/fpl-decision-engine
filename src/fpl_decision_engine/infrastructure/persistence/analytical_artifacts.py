"""Filesystem persistence for immutable content-addressed analytical artefacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from fpl_decision_engine.domain.analytical_artifact import (
    ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
    AnalyticalArtifact,
)
from fpl_decision_engine.ports.analytical_artifacts import (
    AnalyticalArtifactConflict,
    InvalidAnalyticalArtifact,
    PersistedAnalyticalArtifact,
)


def serialize_analytical_artifact(artifact: AnalyticalArtifact) -> bytes:
    """Return canonical UTF-8 JSON bytes for one analytical artefact."""

    payload = artifact.model_dump(mode="json")
    payload["created_at"] = artifact.created_at_utc
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def parse_analytical_artifact(content: bytes) -> AnalyticalArtifact:
    """Parse and validate the supported analytical artefact wire contract."""

    try:
        decoded: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidAnalyticalArtifact(f"analytical artefact is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise InvalidAnalyticalArtifact("analytical artefact must contain a JSON object")
    payload = cast(dict[str, object], decoded)
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise InvalidAnalyticalArtifact("analytical artefact schema_version must be an integer")
    if version != ANALYTICAL_ARTIFACT_SCHEMA_VERSION:
        raise InvalidAnalyticalArtifact(
            f"unsupported analytical artefact schema_version {version}; supported: "
            f"{ANALYTICAL_ARTIFACT_SCHEMA_VERSION}"
        )
    try:
        return AnalyticalArtifact.model_validate(payload)
    except ValidationError as exc:
        raise InvalidAnalyticalArtifact(f"invalid analytical artefact: {exc}") from exc


class FileAnalyticalArtifactRepository:
    """Publish analytical JSON once using same-filesystem hard-link semantics."""

    def __init__(self, state_root: Path = Path("state")) -> None:
        self._root = (state_root / "analytical-artifacts").resolve()

    def publish(self, artifact: AnalyticalArtifact) -> PersistedAnalyticalArtifact:
        """Publish once; an equivalent winner is idempotent and conflicts never replace."""

        content = serialize_analytical_artifact(artifact)
        path = self._path(artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                existing_content = path.read_bytes()
                if existing_content != content:
                    self._require_semantically_identical(
                        expected=artifact,
                        existing_content=existing_content,
                        path=path,
                    )
        finally:
            temporary_path.unlink(missing_ok=True)
        persisted_content = path.read_bytes()
        return PersistedAnalyticalArtifact(
            analysis_artifact_id=artifact.analysis_artifact_id,
            reference=str(path),
            sha256=hashlib.sha256(persisted_content).hexdigest(),
        )

    def load(self, analysis_artifact_id: str) -> AnalyticalArtifact | None:
        """Load one artefact by identity, rejecting malformed or mislocated content."""

        path = self._path_for_id(analysis_artifact_id)
        if not path.exists():
            return None
        artifact = parse_analytical_artifact(path.read_bytes())
        if artifact.analysis_artifact_id != analysis_artifact_id:
            raise InvalidAnalyticalArtifact(
                f"analytical artefact at {path} asserts {artifact.analysis_artifact_id}, "
                f"expected {analysis_artifact_id}"
            )
        return artifact

    def _path(self, artifact: AnalyticalArtifact) -> Path:
        digest = artifact.analysis_artifact_id.removeprefix("sha256:")
        return self._root / artifact.artifact_type.value / f"{digest}.json"

    def _path_for_id(self, analysis_artifact_id: str) -> Path:
        digest = analysis_artifact_id.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise InvalidAnalyticalArtifact(
                "analysis_artifact_id must be a sha256-prefixed lowercase hexadecimal digest"
            )
        matches = tuple(self._root.glob(f"*/{digest}.json"))
        if len(matches) > 1:
            raise InvalidAnalyticalArtifact(
                f"analytical identity {analysis_artifact_id} exists under multiple types"
            )
        return matches[0] if matches else self._root / "unknown" / f"{digest}.json"

    @staticmethod
    def _require_semantically_identical(
        *,
        expected: AnalyticalArtifact,
        existing_content: bytes,
        path: Path,
    ) -> None:
        try:
            existing = parse_analytical_artifact(existing_content)
        except InvalidAnalyticalArtifact as exc:
            raise AnalyticalArtifactConflict(
                f"immutable analytical artefact path contains conflicting bytes: {path}"
            ) from exc
        existing_semantics = existing.model_dump(exclude={"created_at"})
        expected_semantics = expected.model_dump(exclude={"created_at"})
        if existing_semantics != expected_semantics:
            raise AnalyticalArtifactConflict(
                f"immutable analytical artefact path contains conflicting bytes: {path}"
            )
