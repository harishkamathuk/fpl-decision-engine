"""Build, write and read explicit content-addressed GW decision bundle artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from fpl_decision_engine.domain import (
    DecisionBundleV1,
    DecisionInputProvenance,
    DecisionRecommendation,
    DecisionSelection,
    SingleGameweekOptimisationRequest,
    SingleGameweekOptimisationResult,
)


class DecisionBundleError(RuntimeError):
    """A persisted decision bundle is unreadable, invalid or hash-mismatched."""


@dataclass(frozen=True, slots=True)
class DecisionBundleArtifact:
    """Filesystem identity and exact content hash of one immutable bundle artifact."""

    path: Path
    reference: str
    sha256: str


def build_decision_bundle(
    *,
    run_id: UUID,
    decision_at: datetime,
    season: str,
    code_revision: str,
    config_fingerprint: str,
    inputs: DecisionInputProvenance,
    request: SingleGameweekOptimisationRequest,
    result: SingleGameweekOptimisationResult,
) -> DecisionBundleV1:
    """Build a recommendation-only bundle from an existing #6 request and result."""

    recommendation = DecisionRecommendation(
        squad_ids=tuple(sorted((member.player_id for member in result.squad.members), key=str)),
        starting_xi_ids=tuple(sorted(result.starting_xi, key=str)),
        captain_id=result.captain_id,
        vice_captain_id=result.vice_captain_id,
        bench_ids=result.bench,
        formation=result.formation,
        squad_cost_tenths_million=result.squad_cost.tenths_million,
        bank_remaining_tenths_million=result.bank_remaining.tenths_million,
        primary_objective=result.primary_objective,
        solver_status=result.solver_status,
    )
    if not set(recommendation.squad_ids) <= {player.id for player in request.players}:
        raise ValueError("recommendation contains a player outside the optimisation request")
    return DecisionBundleV1(
        decision_run_id=run_id,
        season=season,
        gameweek=request.target_gameweek,
        decision_at=decision_at,
        code_revision=code_revision,
        config_fingerprint=config_fingerprint,
        inputs=inputs,
        recommendation=recommendation,
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _selection_payload(selection: DecisionSelection) -> dict[str, object]:
    return {
        "bench_ids": [str(value) for value in selection.bench_ids],
        "captain_id": str(selection.captain_id),
        "squad_ids": [str(value) for value in selection.squad_ids],
        "starting_xi_ids": [str(value) for value in selection.starting_xi_ids],
        "vice_captain_id": str(selection.vice_captain_id),
    }


def serialize_decision_bundle(bundle: DecisionBundleV1) -> bytes:
    """Return canonical UTF-8 JSON bytes for hashing and later replay.

    Timestamps are normalised to UTC, UUIDs use their canonical lowercase string form,
    keys are sorted, and insignificant whitespace is omitted. Equal semantic bundles
    therefore produce equal bytes independently of caller input ordering.
    """

    inputs = bundle.inputs
    recommendation = {
        **_selection_payload(bundle.recommendation),
        "bank_remaining_tenths_million": bundle.recommendation.bank_remaining_tenths_million,
        "formation": bundle.recommendation.formation.label,
        "primary_objective": bundle.recommendation.primary_objective,
        "solver_status": bundle.recommendation.solver_status,
        "squad_cost_tenths_million": bundle.recommendation.squad_cost_tenths_million,
    }
    actual_choice = None
    if bundle.actual_choice is not None:
        actual_choice = {
            **_selection_payload(bundle.actual_choice),
            "recorded_at": _timestamp(bundle.actual_choice.recorded_at),
        }
    payload = {
        "actual_choice": actual_choice,
        "code_revision": bundle.code_revision,
        "config_fingerprint": bundle.config_fingerprint,
        "decision_at": _timestamp(bundle.decision_at),
        "decision_run_id": str(bundle.decision_run_id),
        "deviation": (
            {"reasons": list(bundle.deviation.reasons)} if bundle.deviation is not None else None
        ),
        "gameweek": bundle.gameweek.value,
        "inputs": {
            "availability_assessment_reference": inputs.availability_assessment_reference,
            "availability_cutoff_at": (
                _timestamp(inputs.availability_cutoff_at)
                if inputs.availability_cutoff_at is not None
                else None
            ),
            "official_snapshot_id": inputs.official_snapshot_id,
            "official_snapshot_reference": inputs.official_snapshot_reference,
            "official_snapshot_sha256": inputs.official_snapshot_sha256,
            "projection_artifact_reference": inputs.projection_artifact_reference,
            "projection_generated_at": _timestamp(inputs.projection_generated_at),
            "projection_model_version": inputs.projection_model_version,
            "projection_provider": inputs.projection_provider,
            "projection_sha256": inputs.projection_sha256,
            "projection_source": inputs.projection_source,
        },
        "recommendation": recommendation,
        "schema_version": bundle.schema_version,
        "season": bundle.season,
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def write_decision_bundle(
    bundle: DecisionBundleV1,
    *,
    state_root: Path = Path("state"),
) -> DecisionBundleArtifact:
    """Atomically write immutable, content-addressed bundle bytes under local state.

    Recommendation-only and later submitted-choice bundles have different hashes and
    paths, so recording the actual choice cannot overwrite the original evidence.
    """

    content = serialize_decision_bundle(bundle)
    digest = hashlib.sha256(content).hexdigest()
    directory = (
        state_root
        / "decision-bundles"
        / f"season={bundle.season}"
        / f"gameweek={bundle.gameweek.value}"
        / str(bundle.decision_run_id)
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError("content-addressed decision bundle path contains conflicting bytes")
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=directory)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return DecisionBundleArtifact(path=path, reference=str(path), sha256=digest)


def parse_decision_bundle(content: bytes) -> DecisionBundleV1:
    """Parse and validate the canonical persisted decision bundle wire contract.

    The canonical serializer renders ``formation`` as its label string; the read seam
    reconstructs the ``Formation`` fields so the persisted document round-trips through
    the immutable v1 model without redesigning the bundle.
    """

    try:
        decoded: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecisionBundleError(f"decision bundle is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise DecisionBundleError("decision bundle must contain a JSON object")
    payload = cast(dict[str, object], decoded)
    gameweek = payload.get("gameweek")
    if isinstance(gameweek, int) and not isinstance(gameweek, bool):
        payload["gameweek"] = {"value": gameweek}
    recommendation = payload.get("recommendation")
    if isinstance(recommendation, dict):
        recommendation_payload = cast(dict[str, object], recommendation)
        formation = recommendation_payload.get("formation")
        if isinstance(formation, str):
            parts = formation.split("-")
            if len(parts) != 3 or any(not part.isdigit() for part in parts):
                raise DecisionBundleError(
                    "decision bundle recommendation formation is not a valid label: "
                    f"{formation!r}"
                )
            recommendation_payload["formation"] = {
                "defenders": int(parts[0]),
                "midfielders": int(parts[1]),
                "forwards": int(parts[2]),
            }
    try:
        return DecisionBundleV1.model_validate(payload)
    except ValidationError as exc:
        raise DecisionBundleError(f"invalid decision bundle: {exc}") from exc


def load_decision_bundle(*, reference: str, sha256: str) -> DecisionBundleV1:
    """Load one immutable bundle through its recorded content-addressed reference.

    The recorded SHA-256 is verified against the exact persisted bytes before parsing,
    mirroring the write seam's content-addressed semantics. Nothing is ever repaired or
    fabricated from a mismatched or unreadable reference.
    """

    try:
        content = Path(reference).read_bytes()
    except OSError as exc:
        raise DecisionBundleError(
            f"cannot read decision bundle at {reference!r}: {exc}"
        ) from exc
    observed = hashlib.sha256(content).hexdigest()
    if observed != sha256:
        raise DecisionBundleError(
            f"decision bundle at {reference!r} content hash mismatch: expected SHA-256 "
            f"{sha256}, observed {observed}"
        )
    return parse_decision_bundle(content)
