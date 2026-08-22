"""Typed control-plane run record following the Issue #80 architecture contract.

A ``RunRecord`` captures the operational lifecycle of one control-plane Run and is
distinct from the decision-engine ``DecisionRun`` provenance: explicit lineage via
``previous_run_id``, append-only stage attempts, artefact references and hashes,
recorded decisions and explicit authority approval events. Stage and run state
transitions follow the approved Issue #80 invariants; structurally invalid records
cannot be constructed or read back.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from .base import DomainModel


class StageState(StrEnum):
    """Approved stage attempt states from the Issue #80 contract."""

    PENDING = "pending"
    RUNNING = "running"
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    BLOCKED = "blocked"


class RunState(StrEnum):
    """Run-level lifecycle outcome recorded in provenance.

    ``AUTHORITATIVE`` is the only non-terminal outcome and must be backed by an
    explicit recorded authority approval event.
    """

    PROVISIONAL = "provisional"
    COMPLETED = "completed"
    FAILED = "failed"
    AUTHORITATIVE = "authoritative"


class CloseOutcome(StrEnum):
    """Operator-declared outcome when closing a provisional run."""

    COMPLETED = "completed"
    FAILED = "failed"


class StageAttempt(DomainModel):
    """One immutable stage attempt; retries append new attempts rather than rewriting.

    A PENDING attempt records an approved retry that has not started. A RUNNING
    attempt records ``started_at`` and no ``finished_at``. PASS/WARN/FAIL record both
    timestamps; a BLOCKED attempt records ``finished_at`` without ever starting.
    """

    stage: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    status: StageState
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    note: str | None = None
    by: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def timestamp_shape_matches_status(self) -> Self:
        if self.status is StageState.RUNNING:
            if self.started_at is None or self.finished_at is not None:
                raise ValueError(
                    f"stage '{self.stage}' attempt {self.attempt}: RUNNING requires "
                    "started_at and no finished_at"
                )
        elif self.status is StageState.PENDING:
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError(
                    f"stage '{self.stage}' attempt {self.attempt}: PENDING must not "
                    "record started_at or finished_at"
                )
        elif self.status is StageState.BLOCKED:
            if self.started_at is not None or self.finished_at is None:
                raise ValueError(
                    f"stage '{self.stage}' attempt {self.attempt}: BLOCKED records "
                    "finished_at and never starts"
                )
        elif self.started_at is None or self.finished_at is None:
            raise ValueError(
                f"stage '{self.stage}' attempt {self.attempt}: terminal PASS/WARN/FAIL "
                "attempts require started_at and finished_at"
            )
        return self


class RunArtefact(DomainModel):
    """Reference and content hash for one artefact produced by a run."""

    name: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    kind: str | None = None
    recorded_at: AwareDatetime

    @field_validator("sha256")
    @classmethod
    def lowercase_hex_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        return value


class RecordedDecision(DomainModel):
    """One recorded decision reference, for example a decision-bundle artefact."""

    reference: str = Field(min_length=1)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    recorded_at: AwareDatetime
    by: str | None = Field(default=None, min_length=1)
    summary: str | None = None

    @field_validator("sha256")
    @classmethod
    def lowercase_hex_digest(cls, value: str | None) -> str | None:
        if value is not None and any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        return value


class AuthorityEvent(DomainModel):
    """Explicit, attributable operator approval recorded in provenance.

    Issue #80: promotion to authoritative requires explicit operator approval recorded
    in provenance; overrides and approvals are attributable, timestamped and
    reason-bearing.
    """

    approved_at: AwareDatetime
    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RunRecord(DomainModel):
    """Current-format control-plane run record (``schema_version`` 1).

    ``previous_run_id`` is explicit lineage, never derived from filesystem recency.
    ``mandatory_stages`` declares the run contract at creation; a run is only
    ``completed`` when every mandatory stage has an acceptable terminal outcome and no
    mandatory stage remains FAIL/BLOCKED. ``failed`` requires mandatory execution to
    have failed. Only a completed run may become ``authoritative``, and only via a
    recorded ``AuthorityEvent``.
    """

    schema_version: int = 1
    run_id: UUID
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    gameweek: int = Field(ge=1, le=38)
    created_at: AwareDatetime
    previous_run_id: UUID | None = None
    mandatory_stages: tuple[str, ...] = Field(min_length=1)
    state: RunState = RunState.PROVISIONAL
    stage_attempts: tuple[StageAttempt, ...] = ()
    artefacts: tuple[RunArtefact, ...] = ()
    decisions: tuple[RecordedDecision, ...] = ()
    authority_events: tuple[AuthorityEvent, ...] = ()
    closed_at: AwareDatetime | None = None
    code_revision: str | None = None
    config_fingerprint: str | None = None
    diagnostic_summary: str | None = None

    @field_validator("schema_version")
    @classmethod
    def supports_only_v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported run-record schema_version {value}; supported: 1")
        return value

    @field_validator("mandatory_stages")
    @classmethod
    def mandatory_stages_are_clean(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(stage.strip() for stage in value)
        if any(not stage for stage in cleaned):
            raise ValueError("mandatory_stages must not contain blank stage names")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("mandatory_stages must be unique")
        return cleaned

    @model_validator(mode="after")
    def lineage_and_structure_invariants(self) -> Self:
        if self.previous_run_id == self.run_id:
            raise ValueError("previous_run_id cannot reference the run itself")
        if (
            tuple(sorted(self.stage_attempts, key=lambda item: (item.stage, item.attempt)))
            != self.stage_attempts
        ):
            raise ValueError("stage_attempts must be ordered by stage then attempt number")
        per_stage: dict[str, int] = {}
        for attempt in self.stage_attempts:
            expected = per_stage.get(attempt.stage, 0) + 1
            if attempt.attempt != expected:
                raise ValueError(
                    f"stage '{attempt.stage}' attempt numbers must be consecutive from 1; "
                    f"found {attempt.attempt} after {expected - 1}"
                )
            per_stage[attempt.stage] = attempt.attempt
        if len(self.authority_events) > 1:
            raise ValueError("a run record may hold at most one authority event")
        if self.state is RunState.PROVISIONAL:
            if self.closed_at is not None:
                raise ValueError("a provisional run must not record closed_at")
        elif self.closed_at is None:
            raise ValueError(f"state {self.state.value} requires closed_at")
        if (
            self.state in (RunState.COMPLETED, RunState.AUTHORITATIVE)
            and not self.mandatory_stages_acceptable
        ):
            raise ValueError(
                f"state {self.state.value} requires every mandatory stage to have an "
                "acceptable terminal outcome (PASS/WARN) with no mandatory FAIL/BLOCKED "
                "remaining"
            )
        if self.state is RunState.FAILED and not self.mandatory_failure:
            raise ValueError(
                "state failed requires at least one mandatory stage with a FAIL or BLOCKED "
                "latest attempt"
            )
        if self.state is RunState.AUTHORITATIVE and not self.authority_events:
            raise ValueError("state authoritative requires a recorded authority approval event")
        if self.state is not RunState.AUTHORITATIVE and self.authority_events:
            raise ValueError("an authority approval event requires state authoritative")
        return self

    @property
    def mandatory_stages_acceptable(self) -> bool:
        """True when every mandatory stage's latest attempt is PASS or WARN."""
        for stage in self.mandatory_stages:
            latest = self.latest_attempt(stage)
            if latest is None or latest.status not in (StageState.PASS, StageState.WARN):
                return False
        return True

    @property
    def mandatory_failure(self) -> bool:
        """True when any mandatory stage's latest attempt is FAIL or BLOCKED."""
        for stage in self.mandatory_stages:
            latest = self.latest_attempt(stage)
            if latest is not None and latest.status in (StageState.FAIL, StageState.BLOCKED):
                return True
        return False

    def latest_attempt(self, stage: str) -> StageAttempt | None:
        """Return the most recent attempt for ``stage``, if any."""
        for attempt in reversed(self.stage_attempts):
            if attempt.stage == stage:
                return attempt
        return None


class LegacyRunRecord(DomainModel):
    """Best-effort read view of a pre-ledger run record.

    Known fields are parsed only where present and type-compatible; genuinely absent or
    unparseable fields remain None/empty and are never fabricated. The full raw payload
    is preserved for operator inspection, and ``parse_issues`` names fields that were
    present but could not be interpreted.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    format: Literal["legacy"] = "legacy"
    run_id: UUID | None = None
    season: str | None = None
    gameweek: int | None = None
    created_at: AwareDatetime | None = None
    previous_run_id: UUID | None = None
    state: str | None = None
    mandatory_stages: tuple[str, ...] = ()
    stage_attempts: tuple[StageAttempt, ...] = ()
    artefacts: tuple[RunArtefact, ...] = ()
    decisions: tuple[RecordedDecision, ...] = ()
    authority_events: tuple[AuthorityEvent, ...] = ()
    closed_at: AwareDatetime | None = None
    code_revision: str | None = None
    config_fingerprint: str | None = None
    diagnostic_summary: str | None = None
    parse_issues: tuple[str, ...] = ()
    raw: dict[str, object]
