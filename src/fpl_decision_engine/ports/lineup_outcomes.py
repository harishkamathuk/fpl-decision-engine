"""Ports for official realised FPL outcomes and joined validation records."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fpl_decision_engine.domain import JoinedLineupOutcome, RealisedOutcome


class RealisedOutcomeProvider(Protocol):
    """Provide final official outcomes keyed by exact season/Gameweek/player identity."""

    def outcomes(
        self, season: str, gameweek: int
    ) -> Mapping[tuple[str, int, UUID], RealisedOutcome]: ...


class JoinedLineupOutcomeRepository(Protocol):
    """Persist and load deterministic joined validation records."""

    def save_all(self, records: Sequence[JoinedLineupOutcome]) -> None: ...

    def load_all(self, season: str, gameweek: int) -> tuple[JoinedLineupOutcome, ...]: ...


class FinalityContract(Protocol):
    """Resolve the official deadline and finality metadata for a Gameweek."""

    def deadline(self, season: str, gameweek: int) -> datetime: ...
