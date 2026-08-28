"""Immutable contracts for authoritative FPL manager-state observations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel
from .value_objects import GameweekNumber


class ManagerStateFailure(StrEnum):
    """Typed acquisition/verification failures exposed to operations."""

    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    MANAGER_NOT_FOUND = "MANAGER_NOT_FOUND"
    MANAGER_IDENTITY_MISMATCH = "MANAGER_IDENTITY_MISMATCH"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    INCOMPLETE_SUPPORTED_DATA = "INCOMPLETE_SUPPORTED_DATA"


class ManagerVerification(StrEnum):
    """Trust outcome for one acquired manager-state observation."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class ManagerComparison(StrEnum):
    """Optional comparison outcome, only when an expected state is supplied."""

    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RawManagerPick(DomainModel):
    """Minimal source pick retained for audit and semantic reconstruction."""

    element_id: int = Field(gt=0)
    position: int = Field(gt=0, le=15)
    is_captain: bool = False
    is_vice_captain: bool = False


class ManagerStateSnapshot(DomainModel):
    """Immutable normalized state observed from the authenticated FPL account.

    The semantic hash excludes acquisition time and credentials, while provenance
    retains the observation time and endpoint used to obtain the state.
    """

    source_provider: str = Field(min_length=1)
    source_endpoint: str = Field(min_length=1)
    acquired_at_utc: AwareDatetime
    manager_entry_id: int = Field(gt=0)
    authenticated_entry_id: int = Field(gt=0)
    target_event_id: GameweekNumber
    target_deadline_time: AwareDatetime
    raw_picks: tuple[RawManagerPick, ...] = Field(min_length=15, max_length=15)
    squad_player_ids: tuple[int, ...] = Field(min_length=15, max_length=15)
    starting_xi_player_ids: tuple[int, ...] = Field(min_length=11, max_length=11)
    captain_player_id: int = Field(gt=0)
    vice_captain_player_id: int = Field(gt=0)
    reserve_goalkeeper_player_id: int = Field(gt=0)
    ordered_outfield_bench_player_ids: tuple[int, ...] = Field(min_length=3, max_length=3)
    source_picks_last_updated: AwareDatetime | None = None
    bank: int | None = Field(default=None, ge=0)
    free_transfers: int | None = Field(default=None, ge=0)
    transfer_status: str | None = None
    chips: tuple[str, ...] = ()
    active_chip: str | None = None
    purchase_prices: tuple[tuple[int, int], ...] = ()
    selling_prices: tuple[tuple[int, int], ...] = ()
    team_value: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def timestamps_and_identity_are_valid(self) -> Self:
        if self.acquired_at_utc.tzinfo is None or self.acquired_at_utc.utcoffset() is None:
            raise ValueError("acquired_at_utc must be timezone-aware")
        if (
            self.target_deadline_time.tzinfo is None
            or self.target_deadline_time.utcoffset() is None
        ):
            raise ValueError("target_deadline_time must be timezone-aware")
        if (
            self.source_picks_last_updated is not None
            and self.source_picks_last_updated > self.acquired_at_utc
        ):
            raise ValueError("source_picks_last_updated cannot be later than acquired_at_utc")
        if self.manager_entry_id != self.authenticated_entry_id:
            raise ValueError("manager_entry_id must equal authenticated_entry_id")
        return self

    @property
    def semantic_identity(self) -> str:
        """Return a deterministic identity for selection semantics and manager/event."""
        payload = {
            "manager_entry_id": self.manager_entry_id,
            "authenticated_entry_id": self.authenticated_entry_id,
            "target_event_id": self.target_event_id.value,
            "source_provider": self.source_provider,
            "squad_player_ids": self.squad_player_ids,
            "starting_xi_player_ids": self.starting_xi_player_ids,
            "captain_player_id": self.captain_player_id,
            "vice_captain_player_id": self.vice_captain_player_id,
            "reserve_goalkeeper_player_id": self.reserve_goalkeeper_player_id,
            "ordered_outfield_bench_player_ids": self.ordered_outfield_bench_player_ids,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @property
    def acquired_at(self) -> datetime:
        """Compatibility accessor returning the aware acquisition timestamp."""
        return self.acquired_at_utc.astimezone(UTC)


class ManagerStateResult(DomainModel):
    """Acquisition result consumed by the engine and later by #88."""

    snapshot: ManagerStateSnapshot | None = None
    verification: ManagerVerification
    failure: ManagerStateFailure | None = None
    comparison: ManagerComparison = ManagerComparison.NOT_APPLICABLE
    mismatch_details: tuple[str, ...] = ()

    @model_validator(mode="after")
    def result_shape_is_consistent(self) -> Self:
        if self.verification is ManagerVerification.VERIFIED and self.snapshot is None:
            raise ValueError("VERIFIED result requires a snapshot")
        if (
            self.verification is ManagerVerification.UNVERIFIED
            and self.failure is None
            and self.snapshot is None
        ):
            raise ValueError("UNVERIFIED result requires a failure or diagnostic snapshot")
        if self.comparison is ManagerComparison.MISMATCHED and not self.mismatch_details:
            raise ValueError("MISMATCHED result requires mismatch details")
        if self.comparison is not ManagerComparison.MISMATCHED and self.mismatch_details:
            raise ValueError("mismatch details require MISMATCHED comparison")
        return self


def compare_manager_state(
    actual: ManagerStateSnapshot,
    expected: ManagerStateSnapshot | None,
) -> tuple[ManagerComparison, tuple[str, ...]]:
    """Compare only explicit selection-semantic state; never consult filesystem recency."""
    if expected is None:
        return ManagerComparison.NOT_APPLICABLE, ()
    fields = (
        "manager_entry_id",
        "target_event_id",
        "squad_player_ids",
        "starting_xi_player_ids",
        "captain_player_id",
        "vice_captain_player_id",
        "reserve_goalkeeper_player_id",
        "ordered_outfield_bench_player_ids",
    )
    differences = tuple(
        field for field in fields if getattr(actual, field) != getattr(expected, field)
    )
    return (
        (ManagerComparison.MISMATCHED, differences)
        if differences
        else (ManagerComparison.MATCHED, ())
    )
