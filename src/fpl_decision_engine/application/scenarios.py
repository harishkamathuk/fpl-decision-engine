"""Application boundary for validating and canonicalising scenario definitions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from fpl_decision_engine.domain import Player, ScenarioConstraints, ScenarioDefinition


class ScenarioErrorCode(StrEnum):
    """Stable categories for deterministic scenario-ingestion failures."""

    INVALID_STRUCTURE = "invalid_structure"
    UNSUPPORTED_CONSTRAINT = "unsupported_constraint"
    UNRESOLVED_PLAYER = "unresolved_player"
    AMBIGUOUS_PLAYER = "ambiguous_player"
    DUPLICATE_CONSTRAINT = "duplicate_constraint"
    CONTRADICTION = "contradiction"


class ScenarioValidationError(ValueError):
    """Raised when an input scenario cannot become a valid canonical definition."""

    def __init__(self, message: str, *, code: ScenarioErrorCode) -> None:
        super().__init__(message)
        self.code = code


PlayerReference = Annotated[str, Field(min_length=1)]


class ScenarioConstraintInput(BaseModel):
    """Strict input representation for the five supported constraint kinds."""

    model_config = ConfigDict(extra="forbid")

    must_include: tuple[PlayerReference, ...] = ()
    excluded: tuple[PlayerReference, ...] = ()
    forced_starters: tuple[PlayerReference, ...] = ()
    forced_captain: PlayerReference | None = None
    forced_vice_captain: PlayerReference | None = None


class ScenarioDefinitionInput(BaseModel):
    """Fixed JSON adaptor model; it is not the canonical domain representation.

    The top-level constraint fields preserve the operational shape. The explicit
    ``constraints`` wrapper is accepted as an equivalent fixed envelope, but mixing
    both forms is rejected so input precedence cannot be accidental.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str | None = Field(default=None, min_length=1)
    rationale: str | None = Field(default=None, min_length=1)
    created_at: AwareDatetime | None = None
    constraints: ScenarioConstraintInput | None = None
    must_include: tuple[PlayerReference, ...] | None = None
    excluded: tuple[PlayerReference, ...] | None = None
    forced_starters: tuple[PlayerReference, ...] | None = None
    forced_captain: PlayerReference | None = None
    forced_vice_captain: PlayerReference | None = None

    @model_validator(mode="after")
    def reject_mixed_constraint_envelopes(self) -> ScenarioDefinitionInput:
        top_level_fields = {
            "must_include",
            "excluded",
            "forced_starters",
            "forced_captain",
            "forced_vice_captain",
        }
        supplied_top_level = top_level_fields & self.model_fields_set
        if self.constraints is not None and supplied_top_level:
            raise ValueError("scenario must use either top-level or wrapped constraints, not both")
        return self

    def constraint_values(self) -> ScenarioConstraintInput:
        """Return the one fixed input constraint envelope selected by the caller."""

        if self.constraints is not None:
            return self.constraints
        return ScenarioConstraintInput(
            must_include=self.must_include or (),
            excluded=self.excluded or (),
            forced_starters=self.forced_starters or (),
            forced_captain=self.forced_captain,
            forced_vice_captain=self.forced_vice_captain,
        )


_ALLOWED_TOP_LEVEL_FIELDS = {
    "scenario_id",
    "label",
    "description",
    "rationale",
    "created_at",
    "constraints",
    "must_include",
    "excluded",
    "forced_starters",
    "forced_captain",
    "forced_vice_captain",
}
_ALLOWED_CONSTRAINT_FIELDS = {
    "must_include",
    "excluded",
    "forced_starters",
    "forced_captain",
    "forced_vice_captain",
}


def _raise_unknown_fields(
    value: Mapping[str, object], allowed: set[str], *, context: str
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ScenarioValidationError(
            f"unsupported {context} field(s): {', '.join(unknown)}",
            code=ScenarioErrorCode.UNSUPPORTED_CONSTRAINT,
        )


def _parse_input(source: Mapping[str, object] | str | bytes) -> ScenarioDefinitionInput:
    if isinstance(source, Mapping):
        value: object = source
    else:
        try:
            value = json.loads(source)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScenarioValidationError(
                "scenario input must be valid JSON",
                code=ScenarioErrorCode.INVALID_STRUCTURE,
            ) from exc

    if not isinstance(value, Mapping):
        raise ScenarioValidationError(
            "scenario input must be a JSON object",
            code=ScenarioErrorCode.INVALID_STRUCTURE,
        )
    typed_value = cast(Mapping[str, object], value)
    _raise_unknown_fields(typed_value, _ALLOWED_TOP_LEVEL_FIELDS, context="scenario")
    constraints = typed_value.get("constraints")
    if isinstance(constraints, Mapping):
        _raise_unknown_fields(
            cast(Mapping[str, object], constraints),
            _ALLOWED_CONSTRAINT_FIELDS,
            context="constraint",
        )
    try:
        return ScenarioDefinitionInput.model_validate(typed_value)
    except ValidationError as exc:
        raise ScenarioValidationError(
            f"invalid scenario structure: {exc}",
            code=ScenarioErrorCode.INVALID_STRUCTURE,
        ) from exc


class _ExactScenarioPlayerResolver:
    """Resolve direct canonical UUIDs or exact external IDs in one namespace."""

    def __init__(self, players: Sequence[Player], namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("identity namespace must not be blank")
        self._canonical_ids = {player.id for player in players}
        if len(self._canonical_ids) != len(players):
            raise ScenarioValidationError(
                "candidate players contain duplicate canonical IDs",
                code=ScenarioErrorCode.INVALID_STRUCTURE,
            )
        self._external: dict[str, list[UUID]] = {}
        for player in players:
            for external_ref in player.external_refs:
                if external_ref.provider == namespace:
                    self._external.setdefault(external_ref.external_id, []).append(player.id)

    def resolve(self, reference: str) -> UUID:
        try:
            canonical_id = UUID(reference)
        except ValueError:
            canonical_id = None
        if canonical_id is not None and canonical_id in self._canonical_ids:
            return canonical_id

        matches = self._external.get(reference, [])
        if not matches:
            raise ScenarioValidationError(
                f"unresolved scenario player reference {reference!r}",
                code=ScenarioErrorCode.UNRESOLVED_PLAYER,
            )
        if len(matches) != 1:
            raise ScenarioValidationError(
                f"ambiguous scenario player reference {reference!r}",
                code=ScenarioErrorCode.AMBIGUOUS_PLAYER,
            )
        return matches[0]


def _resolve_collection(
    references: Sequence[str], resolver: _ExactScenarioPlayerResolver, constraint_name: str
) -> frozenset[UUID]:
    resolved = tuple(resolver.resolve(reference) for reference in references)
    if len(set(resolved)) != len(resolved):
        raise ScenarioValidationError(
            f"duplicate player in {constraint_name} constraint",
            code=ScenarioErrorCode.DUPLICATE_CONSTRAINT,
        )
    return frozenset(resolved)


def _canonical_constraints(
    source: ScenarioConstraintInput, resolver: _ExactScenarioPlayerResolver
) -> ScenarioConstraints:
    try:
        return ScenarioConstraints(
            must_include=_resolve_collection(source.must_include, resolver, "must_include"),
            excluded=_resolve_collection(source.excluded, resolver, "excluded"),
            forced_starters=_resolve_collection(
                source.forced_starters, resolver, "forced_starters"
            ),
            forced_captain=(
                resolver.resolve(source.forced_captain)
                if source.forced_captain is not None
                else None
            ),
            forced_vice_captain=(
                resolver.resolve(source.forced_vice_captain)
                if source.forced_vice_captain is not None
                else None
            ),
        )
    except ValidationError as exc:
        raise ScenarioValidationError(
            f"scenario constraints contradict: {exc}",
            code=ScenarioErrorCode.CONTRADICTION,
        ) from exc


def parse_scenario_definition(
    source: Mapping[str, object] | str | bytes,
    players: Sequence[Player],
    *,
    identity_namespace: str = "fpl",
) -> ScenarioDefinition:
    """Parse supported JSON and return an immutable canonical scenario definition.

    ``players`` is the already canonical candidate universe. Resolution is exact and
    namespace-scoped; this function has no projection input and therefore cannot mutate
    or reinterpret forecasts.
    """

    parsed = _parse_input(source)
    resolver = _ExactScenarioPlayerResolver(players, identity_namespace)
    constraints = _canonical_constraints(parsed.constraint_values(), resolver)
    try:
        return ScenarioDefinition(
            scenario_id=parsed.scenario_id,
            label=parsed.label,
            constraints=constraints,
            description=parsed.description,
            rationale=parsed.rationale,
            created_at=parsed.created_at,
        )
    except ValidationError as exc:
        raise ScenarioValidationError(
            f"invalid canonical scenario definition: {exc}",
            code=ScenarioErrorCode.INVALID_STRUCTURE,
        ) from exc
