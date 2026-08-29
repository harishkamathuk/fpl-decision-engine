"""Build deterministic, read-only execution summaries from RunRecord provenance."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from fpl_decision_engine.application.decision_bundles import (
    DecisionBundleError,
    load_decision_bundle,
)
from fpl_decision_engine.application.gameweek_evidence import (
    EvidenceDriftError,
    InvalidEvidenceManifest,
    parse_gameweek_evidence_manifest,
    validate_gameweek_evidence_references,
)
from fpl_decision_engine.application.submission_safety import (
    SubmissionSafetyArtifactError,
    load_submission_safety_result,
)
from fpl_decision_engine.domain.run_record import (
    GAMEWEEK_EVIDENCE_ARTEFACT_KIND,
    LegacyRunRecord,
    RunArtefact,
    RunRecord,
    StageAttempt,
    StageState,
)

NOT_RECORDED = "not-recorded"


class SafetyAuthorityState(StrEnum):
    """Provenance state of a submission-safety artefact in the derived view."""

    PROVEN = "PROVEN"
    NOT_PROVEN = "NOT_PROVEN"
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"


class SummaryWarningKind(StrEnum):
    """Stable categories for warnings surfaced by derived summary generation."""

    MISSING = "missing"
    LOAD_FAILURE = "load-failure"
    VALIDATION_FAILURE = "validation-failure"
    STAGE = "stage"
    DIAGNOSTIC = "diagnostic"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class SummaryWarning:
    """One deterministic warning or omission reason."""

    kind: SummaryWarningKind
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "message": self.message}


@dataclass(frozen=True, slots=True)
class StageAttemptSummary:
    """Derived view of one recorded stage attempt, including retries."""

    stage: str
    attempt: int
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float | None
    by: str
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "attempt": self.attempt,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "by": self.by,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class StageExecutionSummary:
    """All attempts and latest derived state for one stage."""

    stage: str
    attempts: tuple[StageAttemptSummary, ...]
    latest_status: str
    latest_attempt: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "latest_status": self.latest_status,
            "latest_attempt": self.latest_attempt,
        }


@dataclass(frozen=True, slots=True)
class ArtefactSummary:
    """Exact run-level artefact reference; no stage ownership is inferred."""

    name: str
    kind: str
    reference: str
    sha256: str
    recorded_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "reference": self.reference,
            "sha256": self.sha256,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """Recorded decision enriched only by its explicitly referenced bundle."""

    decision_run_id: str
    reference: str
    hash: str
    recorded_at: str
    by: str
    summary: str
    recommendation: dict[str, object] | None
    primary_objective: float | None
    formation: str | None
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_run_id": self.decision_run_id,
            "reference": self.reference,
            "hash": self.hash,
            "recorded_at": self.recorded_at,
            "by": self.by,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "primary_objective": self.primary_objective,
            "formation": self.formation,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    """Reproducible derived view over one current or legacy RunRecord."""

    run: dict[str, object]
    evidence: dict[str, object]
    comparison_status: str
    stages: tuple[StageExecutionSummary, ...]
    artefacts: tuple[ArtefactSummary, ...]
    decision: DecisionSummary | None
    submission_safety: tuple[dict[str, object], ...]
    authority: dict[str, object]
    warnings: tuple[SummaryWarning, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a stable-key machine representation."""
        return {
            "run": self.run,
            "evidence": self.evidence,
            "comparison_status": self.comparison_status,
            "stages": [stage.to_dict() for stage in self.stages],
            "artefacts": [artefact.to_dict() for artefact in self.artefacts],
            "decision": self.decision.to_dict() if self.decision else None,
            "submission_safety": list(self.submission_safety),
            "authority": self.authority,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    def model_dump(self) -> dict[str, object]:
        """Provide the familiar model serialization name without introducing persistence."""
        return self.to_dict()


def _timestamp(value: datetime | None) -> str:
    return value.isoformat() if value is not None else NOT_RECORDED


def _text(value: str | None) -> str:
    return value if value is not None else NOT_RECORDED


def _duration(attempt: StageAttempt) -> float | None:
    if (
        attempt.status in (StageState.PASS, StageState.WARN, StageState.FAIL)
        and attempt.started_at is not None
        and attempt.finished_at is not None
    ):
        return (attempt.finished_at - attempt.started_at).total_seconds()
    return None


def _attempt_summary(attempt: StageAttempt) -> StageAttemptSummary:
    return StageAttemptSummary(
        stage=attempt.stage,
        attempt=attempt.attempt,
        status=attempt.status.value,
        started_at=_timestamp(attempt.started_at),
        finished_at=_timestamp(attempt.finished_at),
        duration_seconds=_duration(attempt),
        by=_text(attempt.by),
        note=_text(attempt.note),
    )


def _stage_summaries(record: RunRecord | LegacyRunRecord) -> tuple[StageExecutionSummary, ...]:
    attempts_by_stage: dict[str, list[StageAttempt]] = {}
    for attempt in record.stage_attempts:
        attempts_by_stage.setdefault(attempt.stage, []).append(attempt)
    names = set(attempts_by_stage) | set(record.mandatory_stages)
    ordered = tuple(
        sorted(
            names,
            key=lambda name: (
                record.mandatory_stages.index(name)
                if name in record.mandatory_stages
                else len(record.mandatory_stages),
                name,
            ),
        )
    )
    result: list[StageExecutionSummary] = []
    for name in ordered:
        attempts = tuple(_attempt_summary(item) for item in attempts_by_stage.get(name, ()))
        latest = attempts[-1] if attempts else None
        result.append(
            StageExecutionSummary(
                stage=name,
                attempts=attempts,
                latest_status=latest.status if latest else NOT_RECORDED,
                latest_attempt=latest.attempt if latest else None,
            )
        )
    return tuple(result)


def _artefact_summary(artefact: RunArtefact) -> ArtefactSummary:
    return ArtefactSummary(
        name=artefact.name,
        kind=_text(artefact.kind),
        reference=artefact.reference,
        sha256=artefact.sha256,
        recorded_at=_timestamp(artefact.recorded_at),
    )


def _warning(kind: SummaryWarningKind, message: str) -> SummaryWarning:
    return SummaryWarning(kind=kind, message=message)


def _state_string(record: RunRecord | LegacyRunRecord) -> str | None:
    if isinstance(record, RunRecord):
        return record.state.value
    return record.state


def _record_run_fields(record: RunRecord | LegacyRunRecord) -> dict[str, object]:
    state = _state_string(record)
    return {
        "run_id": str(record.run_id) if record.run_id is not None else NOT_RECORDED,
        "season": _text(record.season),
        "gameweek": record.gameweek if record.gameweek is not None else None,
        "previous_run_id": str(record.previous_run_id) if record.previous_run_id else None,
        "state": _text(state),
        "created_at": _timestamp(record.created_at),
        "closed_at": _timestamp(record.closed_at),
        "trigger": NOT_RECORDED,
        "code_revision": _text(record.code_revision),
        "config_fingerprint": _text(record.config_fingerprint),
        "evidence_identity": _text(record.evidence_identity),
        "scenario_status": NOT_RECORDED,
    }


def _decision_summary(
    record: RunRecord | LegacyRunRecord,
    warnings: list[SummaryWarning],
    *,
    decision_loader: Callable[..., Any],
) -> DecisionSummary | None:
    if not record.decisions:
        warnings.append(_warning(SummaryWarningKind.MISSING, "decision: not-recorded"))
        return None
    recorded = record.decisions[-1]
    digest = recorded.sha256 if hasattr(recorded, "sha256") else None
    if digest is None:
        warnings.append(_warning(SummaryWarningKind.MISSING, "decision hash: not-recorded"))
        digest = NOT_RECORDED
    bundle = None
    status = "recorded"
    if digest != NOT_RECORDED:
        try:
            bundle = decision_loader(reference=recorded.reference, sha256=digest)
        except (DecisionBundleError, OSError, ValueError) as exc:
            status = "recorded-but-unavailable"
            warnings.append(_warning(SummaryWarningKind.LOAD_FAILURE, f"decision bundle: {exc}"))
    recommendation: dict[str, object] | None = None
    objective = None
    formation = None
    decision_run_id = NOT_RECORDED
    if bundle is not None:
        recommendation_model = bundle.recommendation
        decision_run_id = str(bundle.decision_run_id)
        objective = recommendation_model.primary_objective
        formation = recommendation_model.formation.label
        recommendation = {
            "squad_ids": [str(value) for value in recommendation_model.squad_ids],
            "starting_xi_ids": [str(value) for value in recommendation_model.starting_xi_ids],
            "captain_id": str(recommendation_model.captain_id),
            "vice_captain_id": str(recommendation_model.vice_captain_id),
            "bench_ids": [str(value) for value in recommendation_model.bench_ids],
            "solver_status": recommendation_model.solver_status,
        }
    return DecisionSummary(
        decision_run_id=decision_run_id,
        reference=recorded.reference,
        hash=digest,
        recorded_at=_timestamp(recorded.recorded_at),
        by=_text(recorded.by),
        summary=_text(recorded.summary),
        recommendation=recommendation,
        primary_objective=objective,
        formation=formation,
        status=status,
    )


def _evidence_summary(
    record: RunRecord | LegacyRunRecord,
    warnings: list[SummaryWarning],
    *,
    evidence_loader: Callable[[str], bytes],
) -> dict[str, object]:
    result: dict[str, object] = {
        "identity": _text(record.evidence_identity),
        "artefact": None,
        "provider": NOT_RECORDED,
        "snapshot_identity": NOT_RECORDED,
        "observed_at": NOT_RECORDED,
        "projection_source": NOT_RECORDED,
        "projection_provider": NOT_RECORDED,
        "generated_at": NOT_RECORDED,
        "model_version": NOT_RECORDED,
    }
    artefact = next(
        (item for item in record.artefacts if item.kind == GAMEWEEK_EVIDENCE_ARTEFACT_KIND),
        None,
    )
    if artefact is None:
        warnings.append(
            _warning(
                SummaryWarningKind.MISSING,
                "evidence manifest artefact: not-recorded",
            )
        )
        return result
    result["artefact"] = _artefact_summary(artefact).to_dict()
    try:
        content = evidence_loader(artefact.reference)
        manifest = parse_gameweek_evidence_manifest(content)
        validate_gameweek_evidence_references(
            manifest,
            evidence_loader,
            claimed_evidence_identity=record.evidence_identity,
        )

    except (
        OSError,
        KeyError,
        InvalidEvidenceManifest,
        EvidenceDriftError,
        ValueError,
    ) as exc:
        warnings.append(
            _warning(SummaryWarningKind.VALIDATION_FAILURE, f"evidence manifest: {exc}")
        )
        return result
    result.update(
        {
            "provider": manifest.snapshot.provider_id,
            "snapshot_identity": manifest.snapshot.snapshot_id,
            "observed_at": manifest.snapshot.observed_at.isoformat(),
            "projection_source": manifest.projection.source,
            "projection_provider": manifest.projection.provider_id,
            "generated_at": manifest.projection.generated_at.isoformat(),
            "model_version": manifest.projection.model_version,
        }
    )
    return result


def _latest_status(record: RunRecord | LegacyRunRecord, stage: str) -> str:
    if not isinstance(record, RunRecord):
        return NOT_RECORDED
    latest = record.latest_attempt(stage)
    return latest.status.value if latest is not None else NOT_RECORDED


def _safety_summaries(
    record: RunRecord | LegacyRunRecord,
    warnings: list[SummaryWarning],
    *,
    decision_loader: Callable[..., Any],
) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for artefact in record.artefacts:
        if artefact.kind not in ("submission-safety-result-v1", "submission-safety-result-v2"):
            continue
        decision = None
        if record.decisions and hasattr(record.decisions[-1], "sha256"):
            recorded = record.decisions[-1]
            try:
                decision = decision_loader(reference=recorded.reference, sha256=recorded.sha256)
            except (DecisionBundleError, OSError, ValueError) as exc:
                warnings.append(
                    _warning(SummaryWarningKind.LOAD_FAILURE, f"safety DecisionBundle: {exc}")
                )
        try:
            safety = load_submission_safety_result(
                reference=artefact.reference,
                sha256=artefact.sha256,
                expected_decision=decision,
            )
            if artefact.kind == "submission-safety-result-v1":
                authority_state = SafetyAuthorityState.NON_AUTHORITATIVE.value
                historical = True
            elif decision is None:
                authority_state = SafetyAuthorityState.NOT_PROVEN.value
                historical = False
                warnings.append(
                    _warning(
                        SummaryWarningKind.VALIDATION_FAILURE,
                        f"submission safety {artefact.name}: v2 authoritative binding "
                        "could not be proven against the recorded DecisionBundle",
                    )
                )
            else:
                authority_state = SafetyAuthorityState.PROVEN.value
                historical = False
            result.append(
                {
                    "name": artefact.name,
                    "phase": safety.phase,
                    "status": safety.status.value,
                    "blocking": safety.blocking,
                    "details": list(safety.details),
                    "historical": historical,
                    "authoritative_binding": authority_state,
                }
            )
        except (SubmissionSafetyArtifactError, OSError, ValueError) as exc:
            warnings.append(
                _warning(
                    SummaryWarningKind.VALIDATION_FAILURE,
                    f"submission safety {artefact.name}: {exc}",
                )
            )
            result.append(
                {
                    "name": artefact.name,
                    "status": NOT_RECORDED,
                    "blocking": None,
                    "historical": artefact.kind == "submission-safety-result-v1",
                    "authoritative_binding": (
                        SafetyAuthorityState.NON_AUTHORITATIVE.value
                        if artefact.kind == "submission-safety-result-v1"
                        else SafetyAuthorityState.NOT_PROVEN.value
                    ),
                }
            )
    return tuple(result)


def build_execution_summary(
    record: RunRecord | LegacyRunRecord,
    *,
    decision_loader: Callable[..., Any] = load_decision_bundle,
    evidence_loader: Callable[[str], bytes] | None = None,
) -> ExecutionSummary:
    """Build a summary without mutating the record or discovering unreferenced files."""
    warnings: list[SummaryWarning] = []
    if isinstance(record, LegacyRunRecord):
        warnings.append(
            _warning(
                SummaryWarningKind.LEGACY,
                "legacy record: absent fields remain not-recorded",
            )
        )
        warnings.extend(_warning(SummaryWarningKind.LEGACY, issue) for issue in record.parse_issues)
    for attempt in record.stage_attempts:
        if attempt.status in (StageState.WARN, StageState.FAIL, StageState.BLOCKED):
            warnings.append(
                _warning(
                    SummaryWarningKind.STAGE,
                    f"stage {attempt.stage} attempt {attempt.attempt}: "
                    f"{attempt.status.value}",
                )
            )
    if record.diagnostic_summary:
        warnings.append(_warning(SummaryWarningKind.DIAGNOSTIC, record.diagnostic_summary))
    loader: Callable[[str], bytes] = (
        (lambda reference: Path(reference).read_bytes())
        if evidence_loader is None
        else evidence_loader
    )
    evidence = _evidence_summary(record, warnings, evidence_loader=loader)
    decision = _decision_summary(record, warnings, decision_loader=decision_loader)
    authority_event = record.authority_events[0] if record.authority_events else None
    state_value = _state_string(record)
    authority: dict[str, object] = {
        "state": _text(state_value),
        "events": [
            {"approved_at": event.approved_at.isoformat(), "by": event.by, "reason": event.reason}
            for event in record.authority_events
        ],
        "mandatory_stage_status": {
            stage: (
                _latest_status(record, stage)
            )
            for stage in record.mandatory_stages
        },
        "decision_present": decision is not None,
        "closed_at": _timestamp(record.closed_at),
    }
    if authority_event is None and state_value == "authoritative":
        warnings.append(_warning(SummaryWarningKind.MISSING, "authority event: not-recorded"))
    return ExecutionSummary(
        run=_record_run_fields(record),
        evidence=evidence,
        comparison_status=NOT_RECORDED,
        stages=_stage_summaries(record),
        artefacts=tuple(_artefact_summary(item) for item in record.artefacts),
        decision=decision,
        submission_safety=_safety_summaries(record, warnings, decision_loader=decision_loader),
        authority=authority,
        warnings=tuple(warnings),
    )


def render_execution_summary_json(summary: ExecutionSummary) -> str:
    """Render canonical JSON with stable insertion order and formatting."""
    return json.dumps(summary.to_dict(), ensure_ascii=False, indent=2) + "\n"


def render_execution_summary_text(summary: ExecutionSummary) -> str:
    """Render concise deterministic human-readable summary."""
    lines = [
        f"run_id: {summary.run['run_id']}",
        f"season: {summary.run['season']}",
        f"gameweek: {summary.run['gameweek']}",
        f"state: {summary.run['state']}",
        f"previous_run_id: {summary.run['previous_run_id'] or NOT_RECORDED}",
        f"trigger: {summary.run['trigger']}",
        f"created_at: {summary.run['created_at']}",
        f"closed_at: {summary.run['closed_at']}",
        f"code_revision: {summary.run['code_revision']}",
        f"config_fingerprint: {summary.run['config_fingerprint']}",
        f"evidence_identity: {summary.run['evidence_identity']}",
        "stages:",
    ]
    for stage in summary.stages:
        lines.append(
            f"  {stage.stage}: {stage.latest_status} "
            f"(attempt {stage.latest_attempt or NOT_RECORDED})"
        )
        for attempt in stage.attempts:
            duration = (
                attempt.duration_seconds
                if attempt.duration_seconds is not None
                else NOT_RECORDED
            )
            lines.append(
                f"    attempt {attempt.attempt}: {attempt.status} duration={duration}"
            )
    lines.append("artefacts:")
    for artefact in summary.artefacts:
        lines.append(
            f"  {artefact.name}: {artefact.reference} "
            f"sha256={artefact.sha256} kind={artefact.kind}"
        )
    decision_status = (
        summary.decision.status if summary.decision is not None else NOT_RECORDED
    )
    lines.append(f"decision: {decision_status}")
    if summary.decision is not None:
        lines.append(f"  decision_run_id: {summary.decision.decision_run_id}")
        lines.append(f"  reference: {summary.decision.reference}")
        lines.append(f"  hash: {summary.decision.hash}")
        lines.append(f"  summary: {summary.decision.summary}")
        objective = (
            summary.decision.primary_objective
            if summary.decision.primary_objective is not None
            else NOT_RECORDED
        )
        lines.append(f"  objective: {objective}")
        lines.append(f"  formation: {summary.decision.formation or NOT_RECORDED}")
    lines.append("evidence:")
    evidence_keys = (
        "identity",
        "provider",
        "snapshot_identity",
        "observed_at",
        "projection_source",
        "projection_provider",
        "generated_at",
        "model_version",
    )
    for key in evidence_keys:
        lines.append(f"  {key}: {summary.evidence[key]}")
    if summary.evidence["artefact"] is not None:
        evidence_artefact = summary.evidence["artefact"]
        lines.append(f"  artefact: {evidence_artefact}")
    lines.append(f"comparison_status: {summary.comparison_status}")
    lines.append("submission_safety:")
    for safety in summary.submission_safety:
        lines.append(
            f"  {safety.get('name', NOT_RECORDED)}: "
            f"status={safety.get('status', NOT_RECORDED)} "
            f"blocking={safety.get('blocking', NOT_RECORDED)} "
            f"authoritative_binding={safety.get('authoritative_binding', NOT_RECORDED)} "
            f"historical={safety.get('historical', NOT_RECORDED)}"
        )
    lines.append("authority:")
    lines.append(f"  state: {summary.authority['state']}")
    lines.append(
        f"  mandatory_stage_status: {summary.authority['mandatory_stage_status']}"
    )
    lines.append(f"  decision_present: {summary.authority['decision_present']}")
    lines.append(f"  closed_at: {summary.authority['closed_at']}")
    lines.append(f"  events: {summary.authority.get('events', [])}")
    lines.append("warnings:")
    lines.extend(f"  - {item.kind.value}: {item.message}" for item in summary.warnings)
    return "\n".join(lines) + "\n"
