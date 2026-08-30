"""Persistence port for immutable prospective lineup-evidence observations."""

from typing import Protocol

from fpl_decision_engine.domain import LineupEvidenceValidationObservation


class LineupObservationPersistenceError(RuntimeError):
    """Base error for lineup observation persistence failures."""


class LineupObservationConflict(LineupObservationPersistenceError):
    """Logical identity was reused with different immutable content."""


class LineupObservationNotFound(LineupObservationPersistenceError):
    """Requested observation does not exist."""


class LineupObservationUnsupportedSchema(LineupObservationPersistenceError):
    """Stored observation uses an unsupported schema version."""


class LineupEvidenceValidationObservationRepository(Protocol):
    """Persist and load one immutable observation per player/Gameweek."""

    def save(self, observation: LineupEvidenceValidationObservation) -> None: ...

    def get(
        self, season: str, gameweek: int, canonical_player_id: str
    ) -> LineupEvidenceValidationObservation | None: ...
