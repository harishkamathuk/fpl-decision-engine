from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.application import (
    ScenarioLifecycleError,
    prepare_scenario_candidate,
    promote_scenario_candidate,
    review_scenario_candidate,
    serialize_frozen_scenario,
    write_frozen_scenario,
)
from fpl_decision_engine.domain import (
    ScenarioClassification,
    ScenarioClassificationResult,
    ScenarioConstraints,
    ScenarioDefinition,
    ScenarioDisposition,
    ValidScenarioDisposition,
)

P1 = UUID(int=1)
P2 = UUID(int=2)
P3 = UUID(int=3)
KNOWN = frozenset({P1, P2, P3})
SELECTABLE = frozenset({P1, P2})
PROJECTED = frozenset({P1, P2})


def scenario(identifier: str, constraints: ScenarioConstraints) -> ScenarioDefinition:
    return ScenarioDefinition(scenario_id=identifier, label=identifier, constraints=constraints)


def candidate(
    *scenarios: ScenarioDefinition, source: str = "source-a", evidence: str = "evidence-a"
):
    return prepare_scenario_candidate(
        candidate_revision="revision-a",
        source_identity=source,
        evidence_identity=evidence,
        scenarios=tuple(scenarios),
    )


def review(cand):
    return review_scenario_candidate(
        candidate=cand,
        selectable_player_ids=SELECTABLE,
        projection_player_ids=PROJECTED,
        known_player_ids=KNOWN,
    )


def promote(
    cand, reviewed, dispositions=(), *, source="source-a", evidence="evidence-a", root=None
):
    return promote_scenario_candidate(
        candidate=cand,
        reviewed=reviewed,
        dispositions=tuple(dispositions),
        current_source_identity=source,
        current_evidence_identity=evidence,
        state_root=root,
    )


def test_t1_invalid_reference_blocks_promotion() -> None:
    invalid = scenario("invalid", ScenarioConstraints(must_include=frozenset({UUID(int=99)})))
    cand = candidate(invalid)
    reviewed = review(cand)

    assert reviewed[0].classification is ScenarioClassification.INVALID_REFERENCE
    assert reviewed[0].reason_code == "unknown_player_reference"
    with pytest.raises(ScenarioLifecycleError):
        promote(cand, reviewed)


def test_t2_missing_projection_blocks_promotion() -> None:
    missing = scenario("missing", ScenarioConstraints(must_include=frozenset({P3})))
    cand = candidate(missing)
    reviewed = review_scenario_candidate(
        candidate=cand,
        selectable_player_ids=KNOWN,
        projection_player_ids=SELECTABLE,
        known_player_ids=KNOWN,
    )

    assert reviewed[0].classification is ScenarioClassification.MISSING_PROJECTION
    assert reviewed[0].reason_code == "required_projection_missing"
    with pytest.raises(ScenarioLifecycleError):
        promote(cand, reviewed)


@pytest.mark.parametrize("keyword", ["evidence", "source"])
def test_t3_identity_drift_rejects_promotion(keyword: str) -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)
    reviewed = review(cand)
    kwargs = {
        "source": "source-b" if keyword == "source" else "source-a",
        "evidence": "evidence-b" if keyword == "evidence" else "evidence-a",
    }

    with pytest.raises(ScenarioLifecycleError, match="stale"):
        promote(
            cand,
            reviewed,
            [
                ValidScenarioDisposition(
                    scenario_id="valid", disposition=ScenarioDisposition.INCLUDE
                )
            ],
            **kwargs,
        )


def test_t3_explicit_supersession_rejects_promotion() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid).model_copy(update={"superseded": True})

    with pytest.raises(ScenarioLifecycleError, match="superseded"):
        promote(
            cand,
            review(cand),
            [
                ValidScenarioDisposition(
                    scenario_id="valid", disposition=ScenarioDisposition.INCLUDE
                )
            ],
        )


def test_b1_idempotent_publication_preserves_existing_bytes(tmp_path: Path) -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    frozen = promote(
        candidate(valid),
        review(candidate(valid)),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )
    first = write_frozen_scenario(frozen, state_root=tmp_path)
    original = first.read_bytes()

    assert write_frozen_scenario(frozen, state_root=tmp_path) == first
    assert first.read_bytes() == original


def test_b1_identical_concurrent_publication_is_idempotent(tmp_path: Path) -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    frozen = promote(
        candidate(valid),
        review(candidate(valid)),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        paths = tuple(
            executor.map(
                lambda _index: write_frozen_scenario(frozen, state_root=tmp_path),
                range(2),
            )
        )

    assert paths[0] == paths[1]
    assert paths[0].read_bytes() == serialize_frozen_scenario(frozen)


def test_b1_conflicting_publication_cannot_replace_existing_bytes(tmp_path: Path) -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    frozen = promote(
        candidate(valid),
        review(candidate(valid)),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )
    path = write_frozen_scenario(frozen, state_root=tmp_path)
    original = path.read_bytes()
    conflicting = path.with_name(path.name)
    conflicting.write_bytes(b"conflicting bytes")

    with pytest.raises(ScenarioLifecycleError):
        write_frozen_scenario(frozen, state_root=tmp_path)
    assert path.read_bytes() == b"conflicting bytes"
    assert original != path.read_bytes()


def test_t4_frozen_artifact_cannot_be_overwritten(tmp_path: Path) -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)
    frozen = promote(
        cand,
        review(cand),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
        root=tmp_path,
    )
    path = write_frozen_scenario(frozen, state_root=tmp_path)
    original = path.read_bytes()

    assert write_frozen_scenario(frozen, state_root=tmp_path) == path
    assert path.read_bytes() == original
    with pytest.raises(ValidationError):
        frozen.promoted_from = "changed"  # type: ignore[misc]


def test_b2_direct_frozen_linkage_mismatches_are_rejected() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)
    reviewed = review(cand)
    frozen = promote(
        cand,
        reviewed,
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )

    with pytest.raises(ValidationError, match="promoted_from"):
        type(frozen)(
            **frozen.model_dump(exclude={"promoted_from"}),
            promoted_from="sha256:" + "0" * 64,
        )
    with pytest.raises(ValidationError, match="promoted_to"):
        type(frozen)(
            **frozen.model_dump(exclude={"promoted_to"}),
            promoted_to="sha256:" + "0" * 64,
        )


def test_b3_reviewed_content_must_match_candidate_not_only_id() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)
    altered = scenario("valid", ScenarioConstraints(must_include=frozenset({P2})))
    tampered = ScenarioClassificationResult(
        scenario=altered,
        classification=ScenarioClassification.VALID,
        reason_code="distinct_effective_hypothesis",
        reason_detail="scenario is resolvable, applicable, and projection-sufficient",
    )

    with pytest.raises(ScenarioLifecycleError, match="exact candidate"):
        promote(
            cand,
            (tampered,),
            [
                ValidScenarioDisposition(
                    scenario_id="valid", disposition=ScenarioDisposition.INCLUDE
                )
            ],
        )


def test_b4_tampered_inapplicable_reason_cannot_promote() -> None:
    inapplicable = scenario("inapplicable", ScenarioConstraints(must_include=frozenset({P3})))
    cand = candidate(inapplicable)
    original = review(cand)[0]
    tampered = original.model_copy(update={"reason_code": "tampered"})

    with pytest.raises(ScenarioLifecycleError, match="reason"):
        promote(cand, (tampered,))


def test_t5_hirst_unselectable_is_inapplicable_and_excluded_player_noop_is_redundant() -> None:
    required = scenario("hirst-in", ScenarioConstraints(must_include=frozenset({P3})))
    excluded = scenario("hirst-out", ScenarioConstraints(excluded=frozenset({P3})))
    cand = candidate(required, excluded)
    reviewed = review(cand)

    assert reviewed[0].classification is ScenarioClassification.INAPPLICABLE
    assert reviewed[0].reason_code == "required_player_unselectable"
    assert reviewed[1].classification is ScenarioClassification.REDUNDANT
    assert reviewed[1].reason_code == "effective_constraint_noop"
    frozen = promote(cand, reviewed)
    assert tuple(item.scenario.scenario_id for item in frozen.reviewed) == ("hirst-in", "hirst-out")
    assert frozen.executable_scenarios == ()


def test_t6_identical_inputs_have_deterministic_candidate_and_frozen_hashes() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    first = candidate(valid)
    second = candidate(valid)
    first_frozen = promote(
        first,
        review(first),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )
    second_frozen = promote(
        second,
        review(second),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )

    assert first.candidate_hash == second.candidate_hash
    assert first_frozen.frozen_hash == second_frozen.frozen_hash


def test_t7_changed_evidence_requires_a_new_candidate() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    old = candidate(valid)
    with pytest.raises(ScenarioLifecycleError):
        promote(
            old,
            review(old),
            [
                ValidScenarioDisposition(
                    scenario_id="valid", disposition=ScenarioDisposition.INCLUDE
                )
            ],
            evidence="evidence-b",
        )
    new = candidate(valid, evidence="evidence-b")
    assert (
        promote(
            new,
            review(new),
            [
                ValidScenarioDisposition(
                    scenario_id="valid", disposition=ScenarioDisposition.INCLUDE
                )
            ],
            evidence="evidence-b",
        ).evidence_identity
        == "evidence-b"
    )


def test_t8_valid_without_disposition_blocks_promotion() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)

    with pytest.raises(ScenarioLifecycleError, match="every VALID"):
        promote(cand, review(cand))


def test_t9_include_is_executable_and_exclude_is_provenance_only() -> None:
    included = scenario("include", ScenarioConstraints(must_include=frozenset({P1})))
    excluded = scenario("exclude", ScenarioConstraints(must_include=frozenset({P2})))
    cand = candidate(included, excluded)
    dispositions = [
        ValidScenarioDisposition(scenario_id="include", disposition=ScenarioDisposition.INCLUDE),
        ValidScenarioDisposition(scenario_id="exclude", disposition=ScenarioDisposition.EXCLUDE),
    ]
    frozen = promote(cand, review(cand), dispositions)

    assert [item.scenario_id for item in frozen.executable_scenarios] == ["include"]
    assert [item.scenario.scenario_id for item in frozen.reviewed] == ["include", "exclude"]


def test_t10_nonvalid_nonblocking_scenarios_need_no_disposition() -> None:
    inapplicable = scenario("inapplicable", ScenarioConstraints(must_include=frozenset({P3})))
    redundant = scenario("redundant", ScenarioConstraints(excluded=frozenset({P3})))
    frozen = promote(candidate(inapplicable, redundant), review(candidate(inapplicable, redundant)))

    assert frozen.dispositions == ()


def test_t11_tampered_review_cannot_claim_old_candidate_hash() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)
    original = review(cand)[0]
    tampered = ScenarioClassificationResult(
        scenario=original.scenario,
        classification=ScenarioClassification.REDUNDANT,
        reason_code="tampered",
        reason_detail="tampered review",
    )

    with pytest.raises(ScenarioLifecycleError, match="review"):
        promote(cand, (tampered,))


def test_t12_elapsed_time_alone_does_not_make_candidate_stale() -> None:
    valid = scenario("valid", ScenarioConstraints(must_include=frozenset({P1})))
    cand = candidate(valid)
    frozen = promote(
        cand,
        review(cand),
        [ValidScenarioDisposition(scenario_id="valid", disposition=ScenarioDisposition.INCLUDE)],
    )

    assert frozen.promoted_from == cand.candidate_hash
