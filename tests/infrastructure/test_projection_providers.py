from __future__ import annotations

import hashlib
import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fpl_decision_engine.domain import (
    ExternalRef,
    GameweekNumber,
    Money,
    Player,
    Position,
    Projection,
)
from fpl_decision_engine.infrastructure.ingestion import prepare_snapshot
from fpl_decision_engine.infrastructure.providers.fpl_snapshot import map_snapshot
from fpl_decision_engine.infrastructure.providers.projections import (
    FPL_CODE_NAMESPACE,
    FPL_FORECAST_PROVIDER_ID,
    FplForecastCsvAdapter,
    GenericLocalCsvProjectionProvider,
    ProjectionProviderConfiguration,
    ProjectionProviderKind,
    create_projection_provider,
)
from fpl_decision_engine.ports import (
    ProjectionProvider,
    ProviderCapability,
    ProviderDataError,
    ProviderMappingError,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
PROJECTION_FIXTURES = FIXTURE_ROOT / "projections"
OBSERVED_AT = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def canonical_players() -> tuple[Player, ...]:
    return map_snapshot(prepare_snapshot(FIXTURE_ROOT / "fpl_snapshot")).players


def generic_provider(path: Path | None = None) -> GenericLocalCsvProjectionProvider:
    return GenericLocalCsvProjectionProvider(
        path or PROJECTION_FIXTURES / "generic.csv",
        canonical_players(),
        identity_namespace="synthetic-fpl",
        provider_id="local-projections",
        provider_version="csv-v1",
        season="2026-27",
        observed_at=OBSERVED_AT,
    )


def write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "projections.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_generic_csv_import_maps_explicit_ids_and_preserves_optional_values() -> None:
    provider = generic_provider()

    response = provider.projections([GameweekNumber(value=1), GameweekNumber(value=2)])

    assert isinstance(provider, ProjectionProvider)
    assert len(response.data) == 2
    first, second = response.data
    assert first.player_id == canonical_players()[0].id
    assert first.expected_points == -0.25
    assert first.expected_minutes is None
    assert first.appearance_probability is None
    assert first.start_probability is None
    assert first.variance is None
    assert first.p10 is None
    assert second.expected_minutes == 181
    assert second.appearance_probability == 0.94
    assert second.start_probability == 0.72
    assert (second.p10, second.p50, second.p90) == (-1.0, 5.5, 12.0)
    assert provider.descriptor.supports(ProviderCapability.APPEARANCE_PROBABILITY)
    assert provider.descriptor.supports(ProviderCapability.POINT_DISTRIBUTION)


def test_fpl_forecast_maps_exact_code_uncertainty_and_lineage() -> None:
    provider = FplForecastCsvAdapter(
        PROJECTION_FIXTURES / "fpl_forecast_phase9.csv",
        canonical_players(),
        season="2026-27",
        observed_at=OBSERVED_AT,
        provider_version="official-data@abc123",
    )

    response = provider.projections([GameweekNumber(value=1)])

    first, second = response.data
    assert first.player_id == canonical_players()[0].id
    assert second.player_id == canonical_players()[1].id
    assert first.expected_minutes == 135
    assert first.appearance_probability == 0.91
    assert first.start_probability == 0.63
    assert first.variance == 2.25
    assert (first.p10, first.p50, first.p90) == (-2.0, -0.2, 2.5)
    assert first.generated_at == datetime(2026, 8, 11, 6, 30, tzinfo=UTC)
    assert first.source == FPL_FORECAST_PROVIDER_ID
    assert "official-data@abc123" in first.model_version
    assert "model=baseline" in first.model_version
    assert "xpoints_run=xp-run-7" in first.model_version
    assert not hasattr(first, "prob_points_ge_5")


def test_fpl_snapshot_retains_stable_code_as_distinct_external_reference() -> None:
    players = canonical_players()

    assert players[0].external_refs == (
        ExternalRef(provider="synthetic-fpl", external_id="101"),
        ExternalRef(provider=FPL_CODE_NAMESPACE, external_id="101282"),
    )
    assert players[1].external_refs[-1] == ExternalRef(
        provider=FPL_CODE_NAMESPACE, external_id="202383"
    )


def test_projection_domain_allows_gameweek_minutes_and_negative_point_values() -> None:
    projection = Projection(
        player_id=uuid4(),
        gameweek=GameweekNumber(value=2),
        expected_points=-0.5,
        expected_minutes=181,
        p10=-3,
        p50=-0.5,
        p90=4,
        source="test",
        model_version="1",
        generated_at=OBSERVED_AT,
    )

    assert projection.expected_minutes == 181
    assert projection.expected_points == -0.5


@pytest.mark.parametrize("field", ["appearance_probability", "start_probability"])
def test_invalid_probability_is_rejected(field: str) -> None:
    values: dict[str, object] = {
        "player_id": uuid4(),
        "gameweek": GameweekNumber(value=1),
        "expected_points": 1.0,
        "source": "test",
        "model_version": "1",
        "generated_at": OBSERVED_AT,
        field: 1.01,
    }

    with pytest.raises(ValidationError):
        Projection.model_validate(values)


def test_csv_provider_rejects_invalid_probability(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "external_player_id,gameweek,expected_points,source,model_version,generated_at,appearance_probability\n"
        "101,1,2.0,test,v1,2026-08-15T08:00:00Z,1.2\n",
    )

    with pytest.raises(ProviderDataError, match="appearance_probability"):
        generic_provider(path)


def test_fpl_forecast_missing_uncertainty_remains_missing(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "schema_version,season,gameweek,stable_player_id,expected_points,model_variant,data_timestamp\n"
        "phase9_frontend_v1,2026-27,1,player_code_101282,3.2,baseline,2026-08-11T06:30:00Z\n",
    )
    provider = FplForecastCsvAdapter(
        path, canonical_players(), season="2026-27", observed_at=OBSERVED_AT
    )

    projection = provider.projections([GameweekNumber(value=1)]).data[0]
    assert projection.expected_minutes is None
    assert projection.appearance_probability is None
    assert projection.start_probability is None
    assert projection.variance is None
    assert (projection.p10, projection.p50, projection.p90) == (None, None, None)


def test_fpl_forecast_rejects_unsupported_future_schema(tmp_path: Path) -> None:
    source = (PROJECTION_FIXTURES / "fpl_forecast_phase9.csv").read_text(encoding="utf-8")
    path = write_csv(tmp_path, source.replace("phase9_frontend_v1", "phase10_frontend_v1"))

    with pytest.raises(
        ProviderDataError,
        match="unsupported FPL Forecast schema 'phase10_frontend_v1'; supported schema is "
        "'phase9_frontend_v1'",
    ):
        FplForecastCsvAdapter(
            path,
            canonical_players(),
            season="2026-27",
            observed_at=OBSERVED_AT,
        )


def test_duplicate_player_gameweek_rows_are_rejected(tmp_path: Path) -> None:
    header = "external_player_id,gameweek,expected_points,source,model_version,generated_at\n"
    path = write_csv(
        tmp_path,
        header
        + "101,1,2.0,test,v1,2026-08-15T08:00:00Z\n"
        + "101,1,3.0,test,v1,2026-08-15T08:00:00Z\n",
    )

    with pytest.raises(ProviderDataError, match="duplicate projection"):
        generic_provider(path)


def test_unmapped_identity_is_rejected(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "external_player_id,gameweek,expected_points,source,model_version,generated_at\n"
        "999,1,2.0,test,v1,2026-08-15T08:00:00Z\n",
    )

    with pytest.raises(ProviderMappingError, match="unmapped player"):
        generic_provider(path)


def test_ambiguous_identity_is_rejected(tmp_path: Path) -> None:
    first = canonical_players()[0]
    duplicate = Player(
        id=uuid4(),
        team_id=first.team_id,
        first_name="Other",
        last_name="Player",
        web_name="Other",
        position=Position.GOALKEEPER,
        price=Money(tenths_million=40),
        external_refs=(ExternalRef(provider="configured", external_id="same"),),
    )
    original = first.model_copy(
        update={"external_refs": (ExternalRef(provider="configured", external_id="same"),)}
    )
    path = write_csv(
        tmp_path,
        "external_player_id,gameweek,expected_points,source,model_version,generated_at\n"
        "same,1,2.0,test,v1,2026-08-15T08:00:00Z\n",
    )

    with pytest.raises(ProviderMappingError, match="ambiguous player"):
        GenericLocalCsvProjectionProvider(
            path,
            (original, duplicate),
            identity_namespace="configured",
            provider_id="local-projections",
            provider_version="csv-v1",
            season="2026-27",
            observed_at=OBSERVED_AT,
        )


def test_source_hash_observation_and_mapping_fingerprint_are_preserved() -> None:
    path = PROJECTION_FIXTURES / "generic.csv"
    provider = generic_provider(path)
    response = provider.projections([GameweekNumber(value=1)])

    assert response.provenance.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert response.provenance.source_sha256 == provider.source_sha256
    assert response.provenance.mapping_fingerprint == provider.mapping_fingerprint
    assert response.provenance.retrieved_at == OBSERVED_AT
    assert response.freshness.as_of == datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    assert response.provenance.season == "2026-27"
    assert response.provenance.source_reference == str(path.resolve())


def test_provider_is_selected_by_configuration() -> None:
    generic = create_projection_provider(
        ProjectionProviderConfiguration(
            kind=ProjectionProviderKind.GENERIC_CSV,
            path=PROJECTION_FIXTURES / "generic.csv",
            season="2026-27",
            observed_at=OBSERVED_AT,
            provider_id="configured-generic",
            provider_version="1",
            identity_namespace="synthetic-fpl",
        ),
        canonical_players(),
    )
    fpl_forecast = create_projection_provider(
        ProjectionProviderConfiguration(
            kind=ProjectionProviderKind.FPL_FORECAST_CSV,
            path=PROJECTION_FIXTURES / "fpl_forecast_phase9.csv",
            season="2026-27",
            observed_at=OBSERVED_AT,
        ),
        canonical_players(),
    )

    assert isinstance(generic, GenericLocalCsvProjectionProvider)
    assert isinstance(fpl_forecast, FplForecastCsvAdapter)


def test_generic_configuration_requires_explicit_identity_namespace() -> None:
    configuration = ProjectionProviderConfiguration(
        kind=ProjectionProviderKind.GENERIC_CSV,
        path=PROJECTION_FIXTURES / "generic.csv",
        season="2026-27",
        observed_at=OBSERVED_AT,
        provider_id="configured-generic",
        provider_version="1",
    )

    with pytest.raises(ValueError, match="identity_namespace"):
        create_projection_provider(configuration, canonical_players())


def test_local_projection_providers_do_not_use_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("projection provider attempted network access")

    monkeypatch.setattr(socket, "socket", network_forbidden)
    provider = generic_provider()

    assert provider.projections([GameweekNumber(value=1)]).data


def test_fpl_forecast_stable_id_must_use_player_code_prefix(tmp_path: Path) -> None:
    source = (PROJECTION_FIXTURES / "fpl_forecast_phase9.csv").read_text(encoding="utf-8")
    path = write_csv(tmp_path, source.replace("player_code_101282", "101282", 1))

    with pytest.raises(ProviderMappingError, match="player_code_<positive integer>"):
        FplForecastCsvAdapter(
            path,
            canonical_players(),
            season="2026-27",
            observed_at=OBSERVED_AT,
        )
