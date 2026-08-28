"""Application services for scenario lifecycle review and immutable promotion."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import UUID

from fpl_decision_engine.domain.scenario import ScenarioDefinition
from fpl_decision_engine.domain.scenario_lifecycle import (
    FrozenScenarioArtefact,
    PreparedScenarioCandidate,
    ScenarioClassification,
    ScenarioClassificationResult,
    ScenarioDisposition,
    ValidScenarioDisposition,
    calculate_frozen_hash,
    classify_scenarios,
    prepare_candidate,
)


class ScenarioLifecycleError(RuntimeError):
    """Raised when review or promotion violates lifecycle invariants."""


def prepare_scenario_candidate(
    *,
    candidate_revision: str,
    source_identity: str,
    evidence_identity: str,
    scenarios: tuple[ScenarioDefinition, ...],
) -> PreparedScenarioCandidate:
    """Prepare an immutable candidate from a source/draft and evidence identity."""

    return prepare_candidate(
        candidate_revision=candidate_revision,
        source_identity=source_identity,
        evidence_identity=evidence_identity,
        scenarios=scenarios,
    )


def review_scenario_candidate(
    *,
    candidate: PreparedScenarioCandidate,
    selectable_player_ids: frozenset[UUID],
    projection_player_ids: frozenset[UUID],
    known_player_ids: frozenset[UUID],
) -> tuple[ScenarioClassificationResult, ...]:
    """Classify a candidate against one authoritative evidence state."""

    return classify_scenarios(
        candidate=candidate,
        selectable_player_ids=selectable_player_ids,
        projection_player_ids=projection_player_ids,
        known_player_ids=known_player_ids,
    )


def promote_scenario_candidate(
    *,
    candidate: PreparedScenarioCandidate,
    reviewed: tuple[ScenarioClassificationResult, ...],
    dispositions: tuple[ValidScenarioDisposition, ...],
    current_source_identity: str,
    current_evidence_identity: str,
    state_root: Path | None = None,
) -> FrozenScenarioArtefact:
    """Derivationally promote a reviewed candidate, optionally writing immutable bytes."""

    if candidate.superseded:
        raise ScenarioLifecycleError("candidate has been explicitly superseded")
    if candidate.source_identity != current_source_identity:
        raise ScenarioLifecycleError("candidate source identity is stale")
    if candidate.evidence_identity != current_evidence_identity:
        raise ScenarioLifecycleError("candidate evidence identity is stale")
    if tuple(item.scenario for item in reviewed) != candidate.scenarios:
        raise ScenarioLifecycleError("review does not describe the exact candidate revision")
    expected_reasons = {
        ScenarioClassification.INAPPLICABLE: "required_player_unselectable",
        ScenarioClassification.REDUNDANT: "effective_constraint_noop",
        ScenarioClassification.VALID: "distinct_effective_hypothesis",
    }
    if any(
        item.classification in expected_reasons
        and item.reason_code != expected_reasons[item.classification]
        for item in reviewed
    ):
        raise ScenarioLifecycleError("review contains an invalid classification reason")
    blockers = {
        ScenarioClassification.INVALID_REFERENCE,
        ScenarioClassification.MISSING_PROJECTION,
    }
    if any(item.classification in blockers for item in reviewed):
        raise ScenarioLifecycleError("candidate contains a promotion-blocking classification")
    valid_ids = {
        item.scenario.scenario_id
        for item in reviewed
        if item.classification is ScenarioClassification.VALID
    }
    disposition_ids = {item.scenario_id for item in dispositions}
    if disposition_ids != valid_ids:
        raise ScenarioLifecycleError("every VALID scenario must have exactly one disposition")
    if len(dispositions) != len(disposition_ids):
        raise ScenarioLifecycleError("scenario dispositions must be unique")
    executable = tuple(
        item.scenario
        for item in reviewed
        if item.classification is ScenarioClassification.VALID
        and next(d.disposition for d in dispositions if d.scenario_id == item.scenario.scenario_id)
        is ScenarioDisposition.INCLUDE
    )
    promoted_from = candidate.candidate_hash
    frozen_hash = calculate_frozen_hash(
        promoted_from=promoted_from,
        source_identity=candidate.source_identity,
        evidence_identity=candidate.evidence_identity,
        candidate_hash=candidate.candidate_hash,
        reviewed=reviewed,
        dispositions=dispositions,
        executable_scenarios=executable,
    )
    artefact = FrozenScenarioArtefact(
        promoted_from=promoted_from,
        promoted_to=frozen_hash,
        source_identity=candidate.source_identity,
        evidence_identity=candidate.evidence_identity,
        candidate_hash=candidate.candidate_hash,
        reviewed=reviewed,
        dispositions=dispositions,
        executable_scenarios=executable,
        frozen_hash=frozen_hash,
    )
    if state_root is not None:
        write_frozen_scenario(artefact, state_root=state_root)
    return artefact


def serialize_frozen_scenario(artefact: FrozenScenarioArtefact) -> bytes:
    """Serialize exact frozen semantic content canonically."""

    return artefact.model_dump_json(exclude_none=True, indent=None).encode() + b"\n"


def write_frozen_scenario(artefact: FrozenScenarioArtefact, *, state_root: Path) -> Path:
    """Write a content-addressed artefact without allowing overwrite or mutation."""

    content = serialize_frozen_scenario(artefact)
    digest = hashlib.sha256(content).hexdigest()
    directory = state_root / "scenario-frozen"
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
                raise ScenarioLifecycleError(
                    "frozen artefact path contains conflicting bytes"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return path
