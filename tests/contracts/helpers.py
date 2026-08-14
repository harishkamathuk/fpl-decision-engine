"""Reusable contract assertions for provider adapters."""

from collections.abc import Sequence

from fpl_decision_engine.domain import GameweekNumber, Projection
from fpl_decision_engine.ports import (
    ProjectionProvider,
    ProviderCapability,
    ProviderDescriptor,
    ProviderResponse,
)


def assert_provider_response_contract[T](
    response: ProviderResponse[T],
    descriptor: ProviderDescriptor,
) -> None:
    assert response.provenance.provider_id == descriptor.provider_id
    assert response.provenance.provider_version == descriptor.version
    assert response.freshness.as_of.tzinfo is not None
    assert response.provenance.retrieved_at.tzinfo is not None


def assert_projection_provider_contract(
    provider: ProjectionProvider,
    gameweeks: Sequence[GameweekNumber],
) -> tuple[Projection, ...]:
    assert provider.descriptor.supports(ProviderCapability.PROJECTIONS)
    response = provider.projections(gameweeks)
    assert_provider_response_contract(response, provider.descriptor)
    assert all(isinstance(item, Projection) for item in response.data)
    requested = {gameweek.value for gameweek in gameweeks}
    assert all(item.gameweek.value in requested for item in response.data)
    return response.data
