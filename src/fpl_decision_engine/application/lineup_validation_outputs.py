"""Generate reproducible lineup-evidence validation artefacts from frozen #93 records."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from fpl_decision_engine.application.lineup_evidence_evaluation import (
    LineupEvidenceStatisticalEvaluator,
    records_from_joined,
)
from fpl_decision_engine.domain import (
    JoinedLineupOutcome,
    LineupEvidenceStatus,
    LineupEvidenceValidationArtefact,
    LineupValidationDatasetRow,
    OutcomeState,
    calculate_artefact_identity,
    calculate_content_hash,
    calculate_dataset_identity,
)


class LineupValidationOutputError(RuntimeError):
    """Base error for #95 output generation and persistence."""


class LineupValidationOutputConflict(LineupValidationOutputError):
    """A content-addressed output path contains different bytes."""


class InvalidLineupValidationArtefact(LineupValidationOutputError):
    """A serialized #95 artefact is malformed or semantically inconsistent."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LineupValidationOutputError(f"value is not canonical JSON: {exc}") from exc


def canonical_validation_dataset(
    records: list[JoinedLineupOutcome] | tuple[JoinedLineupOutcome, ...],
) -> tuple[LineupValidationDatasetRow, ...]:
    """Project complete joined records into stable, duplicate-free dataset rows."""

    rows = tuple(
        sorted(
            (LineupValidationDatasetRow.from_joined(record) for record in records),
            key=lambda row: row.logical_identity,
        )
    )
    identities = tuple(row.logical_identity for row in rows)
    if len(identities) != len(set(identities)):
        raise ValueError("dataset contains duplicate logical rows")
    return rows


def dataset_identity(dataset: tuple[LineupValidationDatasetRow, ...]) -> str:
    """Hash schema version and complete canonical dataset rows."""

    return calculate_dataset_identity(dataset)


def build_lineup_validation_artefact(
    records: list[JoinedLineupOutcome] | tuple[JoinedLineupOutcome, ...],
    *,
    evaluator: LineupEvidenceStatisticalEvaluator | None = None,
    official_outcome_source_hashes: tuple[tuple[str, str], ...] = (),
) -> LineupEvidenceValidationArtefact:
    """Build #95 from frozen #93 records using the unchanged #94 evaluator."""

    dataset = canonical_validation_dataset(records)
    if not dataset:
        raise ValueError("lineup validation dataset must not be empty")
    statistical_evaluator = evaluator or LineupEvidenceStatisticalEvaluator()
    evaluation = statistical_evaluator.evaluate(records_from_joined(records))
    dataset_hash = dataset_identity(dataset)
    base = LineupEvidenceValidationArtefact.model_construct(
        season=dataset[0].season,
        dataset=dataset,
        dataset_identity=dataset_hash,
        evaluation=evaluation,
        analysis_identity=evaluation.analysis_identity,
        evaluator_input_dataset_identity=evaluation.input_dataset_identity,
        protocol_version=evaluation.protocol_version,
        code_version=evaluation.code_version,
        evidence_vocabulary_version=evaluation.evidence_vocabulary_version,
        official_outcome_source_hashes=tuple(sorted(official_outcome_source_hashes)),
        content_hash="0" * 64,
        artefact_identity="sha256:" + "0" * 64,
    )
    content_hash = calculate_content_hash(base)
    artefact_identity = calculate_artefact_identity(
        base.model_copy(update={"content_hash": content_hash})
    )
    return LineupEvidenceValidationArtefact(
        season=dataset[0].season,
        dataset=dataset,
        dataset_identity=dataset_hash,
        evaluation=evaluation,
        analysis_identity=evaluation.analysis_identity,
        evaluator_input_dataset_identity=evaluation.input_dataset_identity,
        protocol_version=evaluation.protocol_version,
        code_version=evaluation.code_version,
        evidence_vocabulary_version=evaluation.evidence_vocabulary_version,
        official_outcome_source_hashes=tuple(sorted(official_outcome_source_hashes)),
        content_hash=content_hash,
        artefact_identity=artefact_identity,
    )


def serialize_lineup_validation_artefact(
    artefact: LineupEvidenceValidationArtefact,
) -> bytes:
    """Return explicit canonical JSON bytes for a #95 artefact."""

    payload = cast(dict[str, object], artefact.model_dump(mode="json"))
    return _canonical_json(payload) + b"\n"


def parse_lineup_validation_artefact(content: bytes) -> LineupEvidenceValidationArtefact:
    """Parse a #95 artefact and validate its declared schema and identities."""

    try:
        decoded: object = json.loads(content)
        if not isinstance(decoded, dict):
            raise ValueError("artefact must be a JSON object")
        payload = cast(dict[str, object], decoded)
        if payload.get("schema_version") != 1:
            raise ValueError(
                "unsupported lineup validation schema_version "
                f"{payload.get('schema_version')}"
            )
        return LineupEvidenceValidationArtefact.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise InvalidLineupValidationArtefact(f"invalid lineup validation artefact: {exc}") from exc


@dataclass(frozen=True, slots=True)
class LineupValidationOutput:
    """Path and exact byte hash of one persisted #95 artefact."""

    path: Path
    reference: str
    sha256: str


def write_lineup_validation_artefact(
    artefact: LineupEvidenceValidationArtefact,
    *,
    state_root: Path = Path("state"),
) -> LineupValidationOutput:
    """Atomically publish an immutable content-addressed #95 artefact."""

    content = serialize_lineup_validation_artefact(artefact)
    digest = hashlib.sha256(content).hexdigest()
    directory = (state_root / "lineup-evidence-validation" / f"season={artefact.season}").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != content:
            raise LineupValidationOutputConflict(f"conflicting bytes at {path}")
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise LineupValidationOutputConflict(f"conflicting bytes at {path}") from None
        finally:
            temporary.unlink(missing_ok=True)
    return LineupValidationOutput(path=path, reference=str(path), sha256=digest)


def load_lineup_validation_artefact(
    *, reference: str, sha256: str
) -> LineupEvidenceValidationArtefact:
    """Load one #95 artefact after verifying exact bytes and embedded identity."""

    try:
        content = Path(reference).read_bytes()
    except OSError as exc:
        raise InvalidLineupValidationArtefact(f"cannot read artefact {reference!r}: {exc}") from exc
    observed = hashlib.sha256(content).hexdigest()
    if observed != sha256:
        raise InvalidLineupValidationArtefact(
            f"artefact hash mismatch: expected {sha256}, observed {observed}"
        )
    artefact = parse_lineup_validation_artefact(content)
    path = Path(reference)
    if path.name != f"{observed}.json":
        raise InvalidLineupValidationArtefact("artefact filename does not match byte hash")
    if path.parent.name != f"season={artefact.season}":
        raise InvalidLineupValidationArtefact("artefact season directory does not match artefact")
    if (
        calculate_dataset_identity(artefact.dataset, artefact.schema_version)
        != artefact.dataset_identity
    ):
        raise InvalidLineupValidationArtefact("dataset identity does not match dataset")
    if calculate_content_hash(artefact) != artefact.content_hash:
        raise InvalidLineupValidationArtefact("content hash does not match artefact")
    if calculate_artefact_identity(artefact) != artefact.artefact_identity:
        raise InvalidLineupValidationArtefact("artefact identity does not match content")
    return artefact


def render_lineup_validation_summary(artefact: LineupEvidenceValidationArtefact) -> str:
    """Render deterministic human-readable text from structured data only."""

    population = artefact.evaluation.population
    baseline = artefact.evaluation.provider_baseline
    model = artefact.evaluation.incremental_model
    comparison = artefact.evaluation.predictive_comparison
    sufficiency = artefact.evaluation.sample_sufficiency
    lines = [
        "lineup-evidence-validation",
        f"season: {artefact.season}",
        f"dataset_identity: {artefact.dataset_identity}",
        f"analysis_identity: {artefact.analysis_identity}",
        f"protocol_version: {artefact.protocol_version}",
        f"code_version: {artefact.code_version}",
        f"evidence_vocabulary_version: {artefact.evidence_vocabulary_version}",
        f"rows_supplied: {population.supplied}",
        f"rows_primary: {population.primary}",
        "exclusions:",
    ]
    for reason, count in sorted(
        population.exclusions_by_reason.items(), key=lambda item: item[0].value
    ):
        lines.append(f"  {reason.value}: {count}")
    lines.append(
        "  retained_outcome_states: "
        + ", ".join(
            f"{state.value}="
            f"{sum(row.outcome_state is state for row in artefact.dataset)}"
            for state in (OutcomeState.STARTED, OutcomeState.NON_START, OutcomeState.MISSING)
        )
    )
    lines.append(
        "  retained_evidence_states: "
        + ", ".join(
            f"{status.value}="
            f"{sum(row.evidence_status is status for row in artefact.dataset)}"
            for status in LineupEvidenceStatus
        )
    )
    lines.extend([
        f"provider_brier_score: {baseline.brier_score}",
        f"provider_log_loss: {baseline.log_loss}",
        f"provider_mean_p_start: {baseline.mean_p_start}",
        f"realised_start_rate: {baseline.realised_start_rate}",
        "calibration:",
    ])
    for item in artefact.evaluation.calibration:
        lines.append(
            f"  [{item.lower}, {item.upper}]: n={item.n} mean_p_start={item.mean_p_start} "
            f"realised_start_rate={item.realised_start_rate} sparse={item.sparse}"
        )
    lines.append("evidence_classes:")
    for evidence_class, summary in sorted(
        artefact.evaluation.evidence_classes.items(), key=lambda item: item[0].value
    ):
        lines.append(
            f"  {evidence_class.value}: n={summary.n} mean_p_start={summary.mean_p_start} "
            f"realised_start_rate={summary.realised_start_rate} brier_score={summary.brier_score}"
        )
    lines.extend([
        f"regression_converged: {model.converged}",
        "regression_diagnostic: "
        f"{model.diagnostic_reason.value if model.diagnostic_reason else None}",
        f"or_supports_start: {model.or_supports_start}",
        f"or_supports_start_ci95: {model.or_supports_start_ci95}",
        f"or_supports_bench: {model.or_supports_bench}",
        f"or_supports_bench_ci95: {model.or_supports_bench_ci95}",
        f"predictive_delta_brier: {comparison.delta_brier}",
        f"sufficiency_overall: {sufficiency.overall}",
        f"sufficiency_total_n: {sufficiency.total_n_pass}",
        f"sufficiency_distinct_players: {sufficiency.distinct_players_pass}",
        f"sufficiency_supports_start: {sufficiency.supports_start_pass}",
        f"sufficiency_supports_bench: {sufficiency.supports_bench_pass}",
        f"sufficiency_no_material_signal: {sufficiency.no_material_signal_pass}",
        f"sufficiency_estimation: {sufficiency.estimation_pass}",
        f"conclusion: {artefact.evaluation.conclusion.value}",
    ])
    return "\n".join(lines) + "\n"
