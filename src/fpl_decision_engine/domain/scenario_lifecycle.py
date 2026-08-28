"""Immutable scenario candidate review, promotion, and freeze contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from .base import DomainModel
from .scenario import ScenarioDefinition


class ScenarioClassification(StrEnum):
    """Per-scenario classification, ordered by the authoritative decision rules."""

    INVALID_REFERENCE = "invalid_reference"
    INAPPLICABLE = "inapplicable"
    MISSING_PROJECTION = "missing_projection"
    REDUNDANT = "redundant"
    VALID = "valid"


class ScenarioDisposition(StrEnum):
    """Operator disposition permitted only for VALID scenarios."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


class ScenarioClassificationResult(DomainModel):
    """Immutable review result with a stable machine-readable reason."""

    scenario: ScenarioDefinition
    classification: ScenarioClassification
    reason_code: str = Field(min_length=1)
    reason_detail: str = Field(min_length=1)


class ValidScenarioDisposition(DomainModel):
    """Operator decision for one VALID scenario."""

    scenario_id: str = Field(min_length=1)
    disposition: ScenarioDisposition


class PreparedScenarioCandidate(DomainModel):
    """Immutable prepared candidate bound to source and evidence identities."""

    candidate_revision: str = Field(min_length=1)
    source_identity: str = Field(min_length=1)
    evidence_identity: str = Field(min_length=1)
    scenarios: tuple[ScenarioDefinition, ...]
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    superseded: bool = False

    @model_validator(mode="after")
    def identity_matches_content(self) -> Self:
        if len({item.scenario_id for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("candidate scenario IDs must be unique")
        if self.candidate_hash != calculate_candidate_hash(
            source_identity=self.source_identity,
            evidence_identity=self.evidence_identity,
            scenarios=self.scenarios,
        ):
            raise ValueError("candidate_hash does not match candidate semantic content")
        return self


class FrozenScenarioArtefact(DomainModel):
    """Immutable frozen result retaining all review provenance and executable scenarios."""

    promoted_from: str = Field(min_length=1)
    promoted_to: str = Field(min_length=1)
    source_identity: str = Field(min_length=1)
    evidence_identity: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reviewed: tuple[ScenarioClassificationResult, ...]
    dispositions: tuple[ValidScenarioDisposition, ...] = ()
    executable_scenarios: tuple[ScenarioDefinition, ...] = ()
    frozen_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def frozen_content_is_consistent(self) -> Self:
        if self.promoted_from != self.candidate_hash:
            raise ValueError("promoted_from must equal candidate_hash")
        if self.promoted_to != self.frozen_hash:
            raise ValueError("promoted_to must equal frozen_hash")
        valid_ids = {
            item.scenario.scenario_id
            for item in self.reviewed
            if item.classification is ScenarioClassification.VALID
        }
        disposition_ids = {item.scenario_id for item in self.dispositions}
        if disposition_ids != valid_ids:
            raise ValueError("frozen dispositions must cover exactly all VALID scenarios")
        expected_executable = tuple(
            item.scenario
            for item in self.reviewed
            if item.classification is ScenarioClassification.VALID
            and next(
                disposition.disposition
                for disposition in self.dispositions
                if disposition.scenario_id == item.scenario.scenario_id
            )
            is ScenarioDisposition.INCLUDE
        )
        if self.executable_scenarios != expected_executable:
            raise ValueError("executable scenarios do not match dispositions")
        if self.frozen_hash != calculate_frozen_hash(
            promoted_from=self.promoted_from,
            source_identity=self.source_identity,
            evidence_identity=self.evidence_identity,
            candidate_hash=self.candidate_hash,
            reviewed=self.reviewed,
            dispositions=self.dispositions,
            executable_scenarios=self.executable_scenarios,
        ):
            raise ValueError("frozen_hash does not match frozen semantic content")
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _scenario_payload(scenario: ScenarioDefinition) -> dict[str, object]:
    return {
        "scenario_id": scenario.scenario_id,
        "label": scenario.label,
        "description": scenario.description,
        "rationale": scenario.rationale,
        "constraints": {
            "must_include": sorted(map(str, scenario.constraints.must_include)),
            "excluded": sorted(map(str, scenario.constraints.excluded)),
            "forced_starters": sorted(map(str, scenario.constraints.forced_starters)),
            "forced_captain": str(scenario.constraints.forced_captain)
            if scenario.constraints.forced_captain
            else None,
            "forced_vice_captain": str(scenario.constraints.forced_vice_captain)
            if scenario.constraints.forced_vice_captain
            else None,
        },
    }


def calculate_candidate_hash(
    *, source_identity: str, evidence_identity: str, scenarios: tuple[ScenarioDefinition, ...]
) -> str:
    """Fingerprint prepared semantic candidate content, excluding revision metadata."""

    return _digest(
        {
            "source_identity": source_identity,
            "evidence_identity": evidence_identity,
            "scenarios": [_scenario_payload(item) for item in scenarios],
        }
    )


def calculate_frozen_hash(
    *,
    promoted_from: str,
    source_identity: str,
    evidence_identity: str,
    candidate_hash: str,
    reviewed: tuple[ScenarioClassificationResult, ...],
    dispositions: tuple[ValidScenarioDisposition, ...],
    executable_scenarios: tuple[ScenarioDefinition, ...],
) -> str:
    """Fingerprint the complete frozen semantic result and provenance."""

    return _digest(
        {
            "promoted_from": promoted_from,
            "source_identity": source_identity,
            "evidence_identity": evidence_identity,
            "candidate_hash": candidate_hash,
            "reviewed": [
                {
                    "scenario": _scenario_payload(item.scenario),
                    "classification": item.classification,
                    "reason_code": item.reason_code,
                    "reason_detail": item.reason_detail,
                }
                for item in reviewed
            ],
            "dispositions": [
                {"scenario_id": item.scenario_id, "disposition": item.disposition}
                for item in dispositions
            ],
            "executable_scenarios": [_scenario_payload(item) for item in executable_scenarios],
        }
    )


def prepare_candidate(
    *,
    candidate_revision: str,
    source_identity: str,
    evidence_identity: str,
    scenarios: tuple[ScenarioDefinition, ...],
) -> PreparedScenarioCandidate:
    """Create a content-addressed prepared candidate."""

    return PreparedScenarioCandidate(
        candidate_revision=candidate_revision,
        source_identity=source_identity,
        evidence_identity=evidence_identity,
        scenarios=scenarios,
        candidate_hash=calculate_candidate_hash(
            source_identity=source_identity,
            evidence_identity=evidence_identity,
            scenarios=scenarios,
        ),
    )


def classify_scenarios(
    *,
    candidate: PreparedScenarioCandidate,
    selectable_player_ids: frozenset[UUID],
    projection_player_ids: frozenset[UUID],
    known_player_ids: frozenset[UUID],
    baseline_constraints: object | None = None,
) -> tuple[ScenarioClassificationResult, ...]:
    """Classify scenarios in the fixed order using evidence, not optimiser coincidence."""

    del baseline_constraints
    results: list[ScenarioClassificationResult] = []
    for scenario in candidate.scenarios:
        constraints = scenario.constraints
        referenced = constraints.must_include | constraints.excluded | constraints.forced_starters
        if constraints.forced_captain is not None:
            referenced |= {constraints.forced_captain}
        if constraints.forced_vice_captain is not None:
            referenced |= {constraints.forced_vice_captain}
        if not referenced <= known_player_ids:
            results.append(
                ScenarioClassificationResult(
                    scenario=scenario,
                    classification=ScenarioClassification.INVALID_REFERENCE,
                    reason_code="unknown_player_reference",
                    reason_detail="scenario references a player outside authoritative evidence",
                )
            )
            continue
        positive = constraints.required_in_squad
        if positive - selectable_player_ids:
            results.append(
                ScenarioClassificationResult(
                    scenario=scenario,
                    classification=ScenarioClassification.INAPPLICABLE,
                    reason_code="required_player_unselectable",
                    reason_detail=(
                        "a required positive constraint cannot be satisfied by the selectable state"
                    ),
                )
            )
            continue
        if positive - projection_player_ids:
            results.append(
                ScenarioClassificationResult(
                    scenario=scenario,
                    classification=ScenarioClassification.MISSING_PROJECTION,
                    reason_code="required_projection_missing",
                    reason_detail=(
                        "a selectable positive constraint has no mandatory target-GW projection"
                    ),
                )
            )
            continue
        if (
            not constraints.must_include
            and not constraints.forced_starters
            and constraints.forced_captain is None
            and constraints.forced_vice_captain is None
            and constraints.excluded <= (known_player_ids - selectable_player_ids)
        ):
            results.append(
                ScenarioClassificationResult(
                    scenario=scenario,
                    classification=ScenarioClassification.REDUNDANT,
                    reason_code="effective_constraint_noop",
                    reason_detail="constraints do not change baseline feasibility",
                )
            )
            continue
        results.append(
            ScenarioClassificationResult(
                scenario=scenario,
                classification=ScenarioClassification.VALID,
                reason_code="distinct_effective_hypothesis",
                reason_detail="scenario is resolvable, applicable, and projection-sufficient",
            )
        )
    return tuple(results)
