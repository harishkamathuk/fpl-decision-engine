"""Application services for authoritative manager-state acquisition and verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fpl_decision_engine.domain import GameweekNumber, Position
from fpl_decision_engine.domain.manager_state import (
    ManagerStateFailure,
    ManagerStateResult,
    ManagerStateSnapshot,
    ManagerVerification,
    compare_manager_state,
)
from fpl_decision_engine.ports import (
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderManagerIdentityError,
    ProviderManagerNotFoundError,
    ProviderUnavailableError,
)


class ManagerStateSource(Protocol):
    """Operational adapter boundary for authenticated FPL acquisition."""

    def acquire(self, *, entry_id: int, target_event: GameweekNumber) -> ManagerStateSnapshot: ...


class ManagerStateVerificationError(ValueError):
    """Raised when a source snapshot cannot satisfy the approved invariants."""


def verify_manager_state(
    snapshot: ManagerStateSnapshot,
    *,
    expected_entry_id: int,
    expected_target_event_id: GameweekNumber,
    known_player_types: dict[int, Position],
    now: datetime | None = None,
) -> ManagerStateResult:
    """Validate identity, event chronology, squad partition and FPL formation legality."""
    try:
        observed_now = (now or datetime.now(UTC)).astimezone(UTC)
        if (
            snapshot.manager_entry_id != expected_entry_id
            or snapshot.authenticated_entry_id != expected_entry_id
        ):
            return ManagerStateResult(
                verification=ManagerVerification.UNVERIFIED,
                failure=ManagerStateFailure.MANAGER_IDENTITY_MISMATCH,
            )
        if snapshot.target_event_id.value not in range(1, 39):
            raise ValueError("target event is outside the FPL range")
        if snapshot.target_event_id != expected_target_event_id:
            raise ValueError("target event is inconsistent with the expected target")
        if snapshot.target_deadline_time.tzinfo is None:
            raise ValueError("target deadline must be timezone-aware")
        if snapshot.acquired_at_utc > observed_now:
            raise ValueError("acquisition timestamp cannot be in the future")
        if len(snapshot.squad_player_ids) != 15 or len(set(snapshot.squad_player_ids)) != 15:
            raise ValueError("squad must contain exactly 15 unique players")
        if any(player_id not in known_player_types for player_id in snapshot.squad_player_ids):
            raise ValueError("squad contains a player absent from bootstrap metadata")
        if (
            len(snapshot.starting_xi_player_ids) != 11
            or len(set(snapshot.starting_xi_player_ids)) != 11
        ):
            raise ValueError("starting XI must contain exactly 11 unique players")
        squad = set(snapshot.squad_player_ids)
        starters = set(snapshot.starting_xi_player_ids)
        if not starters <= squad:
            raise ValueError("starting XI must be contained in squad")
        if (
            snapshot.captain_player_id not in starters
            or snapshot.vice_captain_player_id not in starters
        ):
            raise ValueError("captain and vice-captain must start")
        if snapshot.captain_player_id == snapshot.vice_captain_player_id:
            raise ValueError("captain and vice-captain must differ")
        bench = set(snapshot.ordered_outfield_bench_player_ids) | {
            snapshot.reserve_goalkeeper_player_id
        }
        if (
            len(snapshot.ordered_outfield_bench_player_ids) != 3
            or len(set(snapshot.ordered_outfield_bench_player_ids)) != 3
        ):
            raise ValueError("outfield bench must contain exactly three unique players")
        if (
            snapshot.reserve_goalkeeper_player_id in starters
            or not bench <= squad
            or bench & starters
        ):
            raise ValueError("bench must be four non-starters in the squad")
        if (
            known_player_types[snapshot.reserve_goalkeeper_player_id]
            is not Position.GOALKEEPER
        ):
            raise ValueError("reserve goalkeeper must be identified by bootstrap element type")
        if any(
            known_player_types[player_id] is Position.GOALKEEPER
            for player_id in snapshot.ordered_outfield_bench_player_ids
        ):
            raise ValueError("outfield bench cannot contain a goalkeeper")
        if starters | bench != squad:
            raise ValueError("starting XI and bench must partition squad")
        if len(bench) != 4:
            raise ValueError("bench must contain exactly four players")
        squad_counts = Counter(
            known_player_types[player_id] for player_id in snapshot.squad_player_ids
        )
        if squad_counts != Counter(
            {
                Position.GOALKEEPER: 2,
                Position.DEFENDER: 5,
                Position.MIDFIELDER: 5,
                Position.FORWARD: 3,
            }
        ):
            raise ValueError("squad composition is not legal")
        xi_counts = Counter(known_player_types[player_id] for player_id in starters)
        if (
            xi_counts[Position.GOALKEEPER] != 1
            or xi_counts[Position.DEFENDER] < 3
            or xi_counts[Position.MIDFIELDER] < 2
            or xi_counts[Position.FORWARD] < 1
        ):
            raise ValueError("starting XI formation is not legal")
        return ManagerStateResult(snapshot=snapshot, verification=ManagerVerification.VERIFIED)
    except (KeyError, ValueError):
        return ManagerStateResult(
            snapshot=snapshot,
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.MALFORMED_RESPONSE,
        )


def acquire_manager_state(
    source: ManagerStateSource,
    *,
    entry_id: int,
    target_event: GameweekNumber,
    known_player_types: dict[int, Position],
    expected_snapshot: ManagerStateSnapshot | None = None,
) -> ManagerStateResult:
    """Acquire once, verify once, and never substitute a prior snapshot on failure."""
    try:
        result = verify_manager_state(
            source.acquire(entry_id=entry_id, target_event=target_event),
            expected_entry_id=entry_id,
            expected_target_event_id=target_event,
            known_player_types=known_player_types,
        )
    except ProviderAuthenticationError:
        return ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.AUTHENTICATION_FAILED,
        )
    except ProviderManagerIdentityError:
        return ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.MANAGER_IDENTITY_MISMATCH,
        )
    except ProviderManagerNotFoundError:
        return ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.MANAGER_NOT_FOUND,
        )
    except ProviderDataError:
        return ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.MALFORMED_RESPONSE,
        )
    except ProviderUnavailableError:
        return ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.SOURCE_UNAVAILABLE,
        )
    except Exception:
        return ManagerStateResult(
            verification=ManagerVerification.UNVERIFIED,
            failure=ManagerStateFailure.SOURCE_UNAVAILABLE,
        )
    if result.verification is not ManagerVerification.VERIFIED:
        return result
    assert result.snapshot is not None
    comparison, details = compare_manager_state(result.snapshot, expected_snapshot)
    return result.model_copy(update={"comparison": comparison, "mismatch_details": details})


def serialize_manager_state(snapshot: ManagerStateSnapshot) -> bytes:
    """Serialize the complete artefact canonically, including provenance but not secrets."""
    content = json.dumps(
        snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return (content + "\n").encode()


def write_manager_state(snapshot: ManagerStateSnapshot, *, state_root: Path) -> Path:
    """Publish an immutable content-addressed manager-state artefact without overwrite."""
    content = serialize_manager_state(snapshot)
    digest = hashlib.sha256(content).hexdigest()
    directory = state_root / "manager-state"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=directory)
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
                raise ManagerStateVerificationError(
                    "manager-state artefact hash path contains conflicting bytes"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return path
