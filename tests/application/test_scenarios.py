from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from fpl_decision_engine.application import (
    ScenarioErrorCode,
    ScenarioValidationError,
    parse_scenario_definition,
)
from fpl_decision_engine.domain import (
    ExternalRef,
    GameweekNumber,
    Money,
    Player,
    Position,
    Projection,
)

GENERATED_AT = datetime(2026, 8, 19, 8, tzinfo=UTC)


def canonical_players(*, ambiguous: bool = False) -> tuple[Player, ...]:
    return (
        Player(
            id=UUID(int=1),
            team_id=UUID(int=101),
            first_name="First",
            last_name="One",
            web_name="One",
            position=Position.FORWARD,
            price=Money(tenths_million=100),
            external_refs=(ExternalRef(provider="fpl", external_id="101"),),
        ),
        Player(
            id=UUID(int=2),
            team_id=UUID(int=102),
            first_name="First",
            last_name="Two",
            web_name="Two",
            position=Position.MIDFIELDER,
            price=Money(tenths_million=90),
            external_refs=(
                ExternalRef(provider="fpl", external_id="101" if ambiguous else "102"),
            ),
        ),
        Player(
            id=UUID(int=3),
            team_id=UUID(int=103),
            first_name="First",
            last_name="Three",
            web_name="Three",
            position=Position.DEFENDER,
            price=Money(tenths_million=80),
            external_refs=(ExternalRef(provider="fpl", external_id="103"),),
        ),
    )


def scenario_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "scenario_id": "test-scenario",
        "label": "Test scenario",
        "must_include": ["101"],
        "excluded": ["102"],
        "forced_starters": ["103"],
        "forced_captain": "101",
        "forced_vice_captain": "103",
    }
    payload.update(updates)
    return payload


def test_valid_input_constructs_canonical_definition_with_all_five_constraints() -> None:
    definition = parse_scenario_definition(scenario_payload(), canonical_players())
    constraints = definition.constraints

    assert definition.scenario_id == "test-scenario"
    assert constraints.must_include == frozenset({UUID(int=1)})
    assert constraints.excluded == frozenset({UUID(int=2)})
    assert constraints.forced_starters == frozenset({UUID(int=3)})
    assert constraints.forced_captain == UUID(int=1)
    assert constraints.forced_vice_captain == UUID(int=3)
    assert constraints.required_starters == frozenset({UUID(int=1), UUID(int=3)})
    assert all(
        isinstance(reference, UUID)
        for values in (
            constraints.must_include,
            constraints.excluded,
            constraints.forced_starters,
        )
        for reference in values
    )  # UUIDs, not external aliases, are stored canonically.


def test_wrapped_input_is_compatible_with_top_level_input() -> None:
    top_level = parse_scenario_definition(scenario_payload(), canonical_players())
    wrapped = parse_scenario_definition(
        {
            "scenario_id": "test-scenario",
            "label": "Test scenario",
            "constraints": {
                "must_include": ["101"],
                "excluded": ["102"],
                "forced_starters": ["103"],
                "forced_captain": "101",
                "forced_vice_captain": "103",
            },
        },
        canonical_players(),
    )

    assert top_level == wrapped


def test_direct_canonical_uuid_reference_is_accepted() -> None:
    definition = parse_scenario_definition(
        scenario_payload(must_include=[str(UUID(int=1))]), canonical_players()
    )

    assert definition.constraints.must_include == frozenset({UUID(int=1)})


@pytest.mark.parametrize(
    ("payload_update", "code", "message"),
    [
        (
            {"must_include": ["101", "101"]},
            ScenarioErrorCode.DUPLICATE_CONSTRAINT,
            "duplicate player in must_include",
        ),
        (
            {"must_include": ["101"], "excluded": ["101"]},
            ScenarioErrorCode.CONTRADICTION,
            "must_include and excluded",
        ),
        (
            {"must_include": [], "forced_starters": ["101"], "excluded": ["101"]},
            ScenarioErrorCode.CONTRADICTION,
            "forced_starters and excluded",
        ),
        (
            {"must_include": [], "forced_captain": "101", "excluded": ["101"]},
            ScenarioErrorCode.CONTRADICTION,
            "forced_captain and excluded",
        ),
        (
            {
                "must_include": [],
                "forced_captain": None,
                "forced_vice_captain": "101",
                "excluded": ["101"],
            },
            ScenarioErrorCode.CONTRADICTION,
            "forced_vice_captain and excluded",
        ),
        (
            {"forced_captain": "101", "forced_vice_captain": "101"},
            ScenarioErrorCode.CONTRADICTION,
            "forced_captain and forced_vice_captain",
        ),
    ],
)
def test_duplicate_and_direct_contradiction_validation(
    payload_update: dict[str, object],
    code: ScenarioErrorCode,
    message: str,
) -> None:
    with pytest.raises(ScenarioValidationError, match=message) as error:
        parse_scenario_definition(scenario_payload(**payload_update), canonical_players())

    assert error.value.code is code


def test_valid_redundant_implied_constraints_remain_valid() -> None:
    definition = parse_scenario_definition(
        scenario_payload(
            must_include=["101"],
            forced_starters=["101"],
            forced_captain="101",
            excluded=[],
            forced_vice_captain=None,
        ),
        canonical_players(),
    )

    assert definition.constraints.required_in_squad == frozenset({UUID(int=1)})


def test_unresolved_and_ambiguous_references_fail_explicitly() -> None:
    with pytest.raises(ScenarioValidationError, match="unresolved") as unresolved:
        parse_scenario_definition(scenario_payload(must_include=["999"]), canonical_players())
    assert unresolved.value.code is ScenarioErrorCode.UNRESOLVED_PLAYER

    with pytest.raises(ScenarioValidationError, match="ambiguous") as ambiguous:
        parse_scenario_definition(
            scenario_payload(must_include=["101"]), canonical_players(ambiguous=True)
        )
    assert ambiguous.value.code is ScenarioErrorCode.AMBIGUOUS_PLAYER


def test_unknown_scenario_fields_and_constraint_types_fail() -> None:
    with pytest.raises(ScenarioValidationError, match="unsupported scenario field") as unknown:
        parse_scenario_definition(scenario_payload(not_a_constraint=["101"]), canonical_players())
    assert unknown.value.code is ScenarioErrorCode.UNSUPPORTED_CONSTRAINT

    with pytest.raises(ScenarioValidationError, match="unsupported constraint field"):
        parse_scenario_definition(
            {
                "scenario_id": "test-scenario",
                "label": "Test scenario",
                "constraints": {"replace_player": {"out": "101", "in": "102"}},
            },
            canonical_players(),
        )


def test_equivalent_input_order_produces_equal_deterministic_definition() -> None:
    first = parse_scenario_definition(
        scenario_payload(must_include=["101", "103"], forced_starters=["103", "101"]),
        canonical_players(),
    )
    second = parse_scenario_definition(
        scenario_payload(must_include=["103", "101"], forced_starters=["101", "103"]),
        canonical_players(),
    )

    assert first == second
    assert first.model_dump() == second.model_dump()


def test_scenario_ingestion_does_not_touch_forecasts() -> None:
    projection = Projection(
        player_id=UUID(int=1),
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        expected_minutes=90,
        source="synthetic",
        model_version="v1",
        generated_at=GENERATED_AT,
    )
    before = projection.model_copy(deep=True)

    parse_scenario_definition(scenario_payload(), canonical_players())

    assert projection == before
    assert projection.expected_points == 7.5
    assert projection.expected_minutes == 90
