"""Offline adapter for FPL-shaped bootstrap and fixture snapshots."""

from .adapter import CanonicalFplSnapshot, map_snapshot
from .availability import FplSnapshotAvailabilityEvidenceProvider
from .provider import FplSnapshotProvider

__all__ = [
    "CanonicalFplSnapshot",
    "FplSnapshotAvailabilityEvidenceProvider",
    "FplSnapshotProvider",
    "map_snapshot",
]
