"""Offline adapter for FPL-shaped bootstrap and fixture snapshots."""

from .adapter import CanonicalFplSnapshot, map_snapshot
from .provider import FplSnapshotProvider

__all__ = ["CanonicalFplSnapshot", "FplSnapshotProvider", "map_snapshot"]
