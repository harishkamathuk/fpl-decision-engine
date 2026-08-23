"""Provider-independent contracts for the control-plane run-record ledger."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from fpl_decision_engine.domain.run_record import LegacyRunRecord, RunRecord
from fpl_decision_engine.ports.persistence import PersistenceError


class RunRecordError(PersistenceError):
    """Base failure raised by the run-record ledger."""


class RunRecordNotFound(RunRecordError):
    """A run record does not exist in the ledger."""


class InvalidRunRecord(RunRecordError):
    """A run record is corrupt, schema-invalid or structurally inconsistent."""


class InvalidStageTransition(RunRecordError):
    """A stage attempt transition violates the Issue #80 stage-state rules."""


class InvalidRunStateTransition(RunRecordError):
    """A run-level lifecycle transition (close/promote/write-after-close) is rejected."""


class InvalidPreviousRunReference(RunRecordError):
    """A previous_run_id does not reference a recorded run."""


class RunRecordConflict(RunRecordError):
    """A concurrent or conflicting write was detected; nothing was committed."""


@runtime_checkable
class RunRecordRepository(Protocol):
    """Persist and read typed control-plane run records.

    ``save`` replaces the whole record document atomically. ``expected_raw`` is the raw
    document the caller loaded before mutating: a create passes ``None`` (the file must
    not exist) and an update passes the previously read bytes, so a concurrent edit is
    detected instead of being silently overwritten.
    """

    def save(self, record: RunRecord, *, expected_raw: str | None = None) -> None: ...

    def get(self, run_id: UUID) -> RunRecord | LegacyRunRecord | None: ...

    def get_raw(self, run_id: UUID) -> str | None: ...

    def list(
        self, *, season: str | None = None, gameweek: int | None = None
    ) -> tuple[RunRecord | LegacyRunRecord, ...]: ...

    def resolve_authoritative_run(self, *, season: str, gameweek: int) -> RunRecord | None: ...
