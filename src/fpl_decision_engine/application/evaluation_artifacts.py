"""Deterministic serialization and content-addressed persistence for evaluation artefacts.

Mirrors the pattern established in decision_bundles.py:
    typed artefact → explicit deterministic serializer → SHA-256 of exact
    serialized bytes → content-addressed JSON artefact → atomic write →
    existing-content verification / no silent overwrite.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fpl_decision_engine.evaluation.contracts import DecisionEvaluationV1


@dataclass(frozen=True, slots=True)
class DecisionEvaluationArtifact:
    """Filesystem identity and exact content hash of one immutable evaluation artefact."""

    path: Path
    reference: str
    sha256: str


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def serialize_decision_evaluation(evaluation: DecisionEvaluationV1) -> bytes:
    """Return canonical UTF-8 JSON bytes for hashing and later replay.

    Timestamps are normalised to UTC, UUIDs use their canonical lowercase
    string form, keys are sorted, and insignificant whitespace is omitted.
    Tuple/list ordering is preserved as-is — scenario order, forecast
    observation order and bench order are semantically meaningful and are
    not sorted.
    """

    baseline = evaluation.baseline
    human = evaluation.human_decision
    comparison = evaluation.comparison
    validation = evaluation.validation
    frozen = evaluation.frozen_input_provenance

    baseline_payload = {
        "baseline_proven_optimal": baseline.baseline_proven_optimal,
        "decision_run_id": str(baseline.decision_run_id),
        "frozen_projection_generated_at": _timestamp(
            baseline.frozen_projection_generated_at
        ),
        "optimiser_status": baseline.optimiser_status,
        "projected_points": baseline.projected_points,
        "projected_vs_realised_residual": baseline.projected_vs_realised_residual,
        "realised_points": baseline.realised_points,
    }

    scenarios_payload: list[dict[str, object]] = []
    for scenario in evaluation.scenarios:
        scenarios_payload.append(
            {
                "decision_run_id": str(scenario.decision_run_id),
                "frozen_projection_generated_at": _timestamp(
                    scenario.frozen_projection_generated_at
                ),
                "optimiser_settings_summary": [
                    {"key": k, "value": v}
                    for k, v in scenario.optimiser_settings_summary
                ],
                "optimiser_status": scenario.optimiser_status,
                "projected_delta_vs_baseline": scenario.projected_delta_vs_baseline,
                "projected_points": scenario.projected_points,
                "projected_vs_realised_residual": scenario.projected_vs_realised_residual,
                "realised_points": scenario.realised_points,
                "scenario_id": scenario.scenario_id,
            }
        )

    human_payload = {
        "projected_delta_vs_baseline": human.projected_delta_vs_baseline,
        "projected_points": human.projected_points,
        "projected_vs_realised_residual": human.projected_vs_realised_residual,
        "rationale_reasons": list(human.rationale_reasons),
        "realised_points": human.realised_points,
        "selection_identity_matches_baseline": human.selection_identity_matches_baseline,
        "selection_identity_matches_scenario_ids": list(
            human.selection_identity_matches_scenario_ids
        ),
    }

    comparison_payload = {
        "projected_override_cost": comparison.projected_override_cost,
        "realised_override_delta": comparison.realised_override_delta,
    }

    validation_payload = {
        "baseline_proven_optimal": validation.baseline_proven_optimal,
        "leakage_checks": list(validation.leakage_checks),
        "optimiser_failure_from_realised_outcome": (
            validation.optimiser_failure_from_realised_outcome
        ),
        "optimiser_status": validation.optimiser_status,
        "same_input_comparison": validation.same_input_comparison,
    }

    observations_payload: list[dict[str, object]] = []
    for obs in evaluation.forecast_observations:
        observations_payload.append(
            {
                "candidate_label": obs.candidate_label,
                "decision_run_id": str(obs.decision_run_id),
                "projected_points": obs.projected_points,
                "realised_points": obs.realised_points,
                "residual": obs.residual,
            }
        )

    frozen_input_payload = {
        "availability_assessment_reference": (
            frozen.availability_assessment_reference
        ),
        "availability_cutoff_at": (
            _timestamp(frozen.availability_cutoff_at)
            if frozen.availability_cutoff_at is not None
            else None
        ),
        "code_revision": frozen.code_revision,
        "official_snapshot_id": frozen.official_snapshot_id,
        "official_snapshot_sha256": frozen.official_snapshot_sha256,
        "projection_generated_at": _timestamp(frozen.projection_generated_at),
        "projection_model_version": frozen.projection_model_version,
        "projection_sha256": frozen.projection_sha256,
    }

    payload = {
        "baseline": baseline_payload,
        "frozen_input_provenance": frozen_input_payload,
        "comparison": comparison_payload,
        "decision_cutoff": _timestamp(evaluation.decision_cutoff),
        "forecast_observations": observations_payload,
        "gameweek": evaluation.gameweek.value,
        "human_decision": human_payload,
        "schema_version": evaluation.schema_version,
        "scenarios": scenarios_payload,
        "season": evaluation.season,
        "validation": validation_payload,
    }

    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def write_decision_evaluation(
    evaluation: DecisionEvaluationV1,
    *,
    state_root: Path = Path("state"),
) -> DecisionEvaluationArtifact:
    """Atomically write immutable, content-addressed evaluation bytes under local state.

    Directory layout mirrors the decision-bundles convention::

        state/decision-evaluations/season=YYYY-YY/gameweek=N/<sha256>.json

    No run_id level is needed because evaluations are uniquely identified by
    their content hash — the same evaluation inputs always produce the same
    path, and differing inputs produce different paths.
    """

    content = serialize_decision_evaluation(evaluation)
    digest = hashlib.sha256(content).hexdigest()
    directory = (
        state_root
        / "decision-evaluations"
        / f"season={evaluation.season}"
        / f"gameweek={evaluation.gameweek.value}"
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(
                "content-addressed evaluation path contains conflicting bytes"
            )
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", dir=directory
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return DecisionEvaluationArtifact(path=path, reference=str(path), sha256=digest)
