from uuid import UUID

import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import ScenarioConstraints, ScenarioDefinition

PLAYER = UUID(int=1)
OTHER = UUID(int=2)


def test_canonical_constraints_expose_semantic_implications() -> None:
    constraints = ScenarioConstraints(
        must_include=frozenset({PLAYER}),
        forced_starters=frozenset(),
        forced_captain=PLAYER,
    )

    assert constraints.must_include == frozenset({PLAYER})
    assert constraints.required_starters == frozenset({PLAYER})
    assert constraints.required_in_squad == frozenset({PLAYER})


def test_scenario_definition_is_immutable_and_canonical() -> None:
    definition = ScenarioDefinition(
        scenario_id="haaland-in",
        label="Include player",
        constraints=ScenarioConstraints(must_include=frozenset({PLAYER})),
    )

    assert definition.scenario_id == "haaland-in"
    with pytest.raises(ValidationError):
        definition.scenario_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("must_include", "must_include and excluded"),
        ("forced_starters", "forced_starters and excluded"),
        ("forced_captain", "forced_captain and excluded"),
        ("forced_vice_captain", "forced_vice_captain and excluded"),
    ],
)
def test_canonical_constraints_reject_each_exclusion_contradiction(
    field: str, message: str
) -> None:
    values: dict[str, object] = {"excluded": frozenset({PLAYER}), field: frozenset({PLAYER})}
    if field in {"forced_captain", "forced_vice_captain"}:
        values[field] = PLAYER

    with pytest.raises(ValidationError, match=message):
        ScenarioConstraints.model_validate(values)


def test_canonical_constraints_reject_captain_and_vice_contradiction() -> None:
    with pytest.raises(ValidationError, match="forced_captain and forced_vice_captain"):
        ScenarioConstraints(forced_captain=PLAYER, forced_vice_captain=PLAYER)


def test_redundant_constraints_are_valid() -> None:
    constraints = ScenarioConstraints(
        must_include=frozenset({PLAYER}),
        forced_starters=frozenset({PLAYER}),
        forced_captain=PLAYER,
    )

    assert constraints.required_in_squad == frozenset({PLAYER})
    assert OTHER not in constraints.required_in_squad
