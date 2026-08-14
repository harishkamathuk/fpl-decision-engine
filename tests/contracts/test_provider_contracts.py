"""Contract tests for provider ports."""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from fpl_decision_engine.domain import GameweekNumber, Projection
from fpl_decision_engine.ports import (
    Freshness,
    ProjectionProvider,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderDescriptor,
    ProviderErrorCode,
    ProviderProvenance,
    ProviderResponse,
)

from .helpers import assert_projection_provider_contract


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


class FakeProjectionProvider:
    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id="fake-projections",
            display_name="Fake projections",
            version="1.0",
            capabilities=frozenset(
                {
                    ProviderCapability.PROJECTIONS,
                    ProviderCapability.EXPECTED_MINUTES,
                    ProviderCapability.START_PROBABILITY,
                }
            ),
        )

    def projections(
        self,
        gameweeks: Sequence[GameweekNumber],
    ) -> ProviderResponse[tuple[Projection, ...]]:
        gameweek = gameweeks[0]
        projection = Projection(
            player_id=uuid4(),
            gameweek=gameweek,
            expected_points=6.2,
            expected_minutes=82,
            start_probability=0.9,
            source=self.descriptor.provider_id,
            model_version=self.descriptor.version,
            generated_at=NOW,
        )
        return ProviderResponse(
            data=(projection,),
            provenance=ProviderProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.version,
                retrieved_at=NOW,
                snapshot_id="fixture-1",
            ),
            freshness=Freshness(as_of=NOW, stale_after=timedelta(hours=6)),
        )


def test_fake_projection_provider_satisfies_protocol_and_contract() -> None:
    provider = FakeProjectionProvider()

    assert isinstance(provider, ProjectionProvider)
    projections = assert_projection_provider_contract(
        provider,
        [GameweekNumber(value=1)],
    )

    assert len(projections) == 1
    assert projections[0].expected_points == 6.2


def test_capabilities_are_explicit() -> None:
    descriptor = FakeProjectionProvider().descriptor

    assert descriptor.supports(ProviderCapability.PROJECTIONS)
    assert descriptor.supports(ProviderCapability.START_PROBABILITY)
    assert not descriptor.supports(ProviderCapability.POINT_DISTRIBUTION)


def test_freshness_reports_staleness_deterministically() -> None:
    freshness = Freshness(as_of=NOW, stale_after=timedelta(hours=2))

    assert not freshness.is_stale_at(NOW + timedelta(hours=2))
    assert freshness.is_stale_at(NOW + timedelta(hours=2, seconds=1))


def test_naive_freshness_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Freshness(as_of=datetime(2026, 8, 14, 12, 0))


def test_provider_error_exposes_machine_readable_semantics() -> None:
    error = ProviderCapabilityError(
        "point distributions are not supported",
        provider_id="fake-projections",
    )

    assert error.code is ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert error.provider_id == "fake-projections"
    assert not error.retryable
