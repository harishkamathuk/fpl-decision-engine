"""Atomic JSON persistence for issue #93 joined lineup outcomes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from fpl_decision_engine.domain import JoinedLineupOutcome


class JoinedOutcomeConflict(RuntimeError):
    """An immutable joined record already exists with different content."""


def serialize_joined_outcome(record: JoinedLineupOutcome) -> bytes:
    """Return deterministic canonical JSON bytes for a joined record."""

    return (
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def parse_joined_outcome(content: bytes) -> JoinedLineupOutcome:
    """Parse a joined record without repairing malformed or conflicting data."""

    try:
        return JoinedLineupOutcome.model_validate(json.loads(content))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ValueError(f"invalid joined lineup outcome: {exc}") from exc


class FileJoinedLineupOutcomeRepository:
    """Persist joined records under season/Gameweek/player identity."""

    def __init__(self, state_root: Path = Path("state")) -> None:
        self._root = (state_root / "joined-lineup-outcomes").resolve()

    def save_all(
        self, records: tuple[JoinedLineupOutcome, ...] | list[JoinedLineupOutcome]
    ) -> None:
        """Atomically persist records; repeated identical writes are idempotent."""

        for record in records:
            path = self._path(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = serialize_joined_outcome(record)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".joined.", suffix=".tmp", dir=path.parent
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
                        raise JoinedOutcomeConflict(f"joined outcome conflicts at {path}") from None
            finally:
                temporary.unlink(missing_ok=True)

    def load_all(self, season: str, gameweek: int) -> tuple[JoinedLineupOutcome, ...]:
        """Load all records in stable player-ID order."""

        directory = self._root / f"season={season}" / f"gameweek={gameweek}"
        if not directory.exists():
            return ()
        records = tuple(
            parse_joined_outcome(path.read_bytes())
            for path in sorted(directory.glob("*.json"))
        )
        if any(record.logical_identity[:2] != (season, gameweek) for record in records):
            raise ValueError("joined outcome identity disagrees with its path")
        return tuple(sorted(records, key=lambda record: record.logical_identity))

    def _path(self, record: JoinedLineupOutcome) -> Path:
        return (
            self._root
            / f"season={record.observation.season}"
            / f"gameweek={record.observation.gameweek.value}"
            / f"{record.observation.canonical_player_id}.json"
        )
