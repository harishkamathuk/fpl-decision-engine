"""Atomic JSON persistence for immutable lineup-evidence observations."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from fpl_decision_engine.domain import LineupEvidenceValidationObservation
from fpl_decision_engine.ports.lineup_evidence_validation import (
    LineupObservationConflict,
    LineupObservationPersistenceError,
    LineupObservationUnsupportedSchema,
)

SCHEMA_VERSION = 1


def serialize_lineup_observation(observation: LineupEvidenceValidationObservation) -> bytes:
    """Return stable canonical JSON bytes for one observation."""

    return (
        json.dumps(
            observation.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def parse_lineup_observation(content: bytes) -> LineupEvidenceValidationObservation:
    """Parse one supported observation without repairing tampered content."""

    try:
        decoded_value = json.loads(content)
        if not isinstance(decoded_value, dict):
            raise ValueError("observation must be a JSON object")
        decoded = cast(dict[str, object], decoded_value)
        if decoded.get("schema_version") != SCHEMA_VERSION:
            raise LineupObservationUnsupportedSchema(
                f"unsupported lineup observation schema_version {decoded.get('schema_version')}"
            )
        return LineupEvidenceValidationObservation.model_validate(decoded)
    except LineupObservationUnsupportedSchema:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise LineupObservationPersistenceError(f"invalid lineup observation: {exc}") from exc


class FileLineupEvidenceValidationObservationRepository:
    """Persist observations once under season/Gameweek/player logical identity."""

    def __init__(self, state_root: Path = Path("state")) -> None:
        self._root = (state_root / "lineup-evidence-validation").resolve()

    def save(self, observation: LineupEvidenceValidationObservation) -> None:
        """Atomically create an observation; identical bytes are idempotent."""

        path = self._path(
            observation.season,
            observation.gameweek.value,
            observation.canonical_player_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        content = serialize_lineup_observation(observation)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".observation.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise LineupObservationConflict(
                        f"immutable observation conflicts at {path}"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)

    def get(
        self, season: str, gameweek: int, canonical_player_id: str
    ) -> LineupEvidenceValidationObservation | None:
        """Load and validate an observation by its explicit logical identity."""

        try:
            player_id = UUID(canonical_player_id)
        except ValueError as exc:
            raise LineupObservationPersistenceError("canonical_player_id must be a UUID") from exc
        path = self._path(season, gameweek, player_id)
        if not path.exists():
            return None
        observation = parse_lineup_observation(path.read_bytes())
        if observation.logical_identity != (season, gameweek, player_id):
            raise LineupObservationPersistenceError("observation identity disagrees with its path")
        return observation

    def _path(self, season: str, gameweek: int, player_id: UUID) -> Path:
        return self._root / f"season={season}" / f"gameweek={gameweek}" / f"{player_id}.json"
