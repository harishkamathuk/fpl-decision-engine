"""Offline adapter for FPL-shaped bootstrap and fixture snapshots."""

from .adapter import CanonicalFplSnapshot, map_snapshot
from .availability import FplSnapshotAvailabilityEvidenceProvider
from .outcomes import (
    FplOutcomeSnapshot,
    FplOutcomeSources,
    OutcomeSnapshotNotFinalError,
    parse_final_fpl_outcomes,
)
from .provider import FplSnapshotProvider

__all__ = [
    "CanonicalFplSnapshot",
    "FplSnapshotAvailabilityEvidenceProvider",
    "FplSnapshotProvider",
    "map_snapshot",
    "FplOutcomeSnapshot",
    "FplOutcomeSources",
    "OutcomeSnapshotNotFinalError",
    "parse_final_fpl_outcomes",
]
