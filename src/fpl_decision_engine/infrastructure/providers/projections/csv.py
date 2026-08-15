"""Deterministic local-CSV projection providers and exact identity resolution."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from fpl_decision_engine.domain import GameweekNumber, Player, Projection
from fpl_decision_engine.ports import (
    Freshness,
    ProjectionProvider,
    ProviderCapability,
    ProviderDataError,
    ProviderDescriptor,
    ProviderMappingError,
    ProviderProvenance,
    ProviderResponse,
)

FPL_CODE_NAMESPACE = "fpl_code"
FPL_FORECAST_PROVIDER_ID = "fpl_forecast"
FPL_FORECAST_SCHEMA_VERSION = "phase9_frontend_v1"
_FPL_FORECAST_ID = re.compile(r"^player_code_([1-9][0-9]*)$")


class GenericProjectionRow(BaseModel):
    """Project-owned CSV row with optional uncertainty left genuinely optional."""

    model_config = ConfigDict(extra="forbid")

    external_player_id: str = Field(min_length=1)
    gameweek: int = Field(ge=1, le=38)
    expected_points: float
    source: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    generated_at: AwareDatetime
    expected_minutes: float | None = Field(default=None, ge=0)
    appearance_probability: float | None = Field(default=None, ge=0, le=1)
    start_probability: float | None = Field(default=None, ge=0, le=1)
    variance: float | None = Field(default=None, ge=0)
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None


class FplForecastRow(BaseModel):
    """Fields consumed from the current phase9 frontend projection artifact.

    Extra columns remain in the user-supplied evidence file and are deliberately ignored.
    In particular, P(5+) and P(10+) are validated when present but are not mapped to
    canonical percentiles because they describe threshold probabilities, not quantiles.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str | int
    season: str = Field(min_length=1)
    gameweek: int = Field(ge=1, le=38)
    stable_player_id: str = Field(min_length=1)
    expected_points: float
    expected_minutes: float | None = Field(default=None, ge=0)
    p_appearance: float | None = Field(default=None, ge=0, le=1)
    p_start: float | None = Field(default=None, ge=0, le=1)
    points_std: float | None = Field(default=None, ge=0)
    points_p10: float | None = None
    points_p50: float | None = None
    points_p90: float | None = None
    prob_points_ge_5: float | None = Field(default=None, ge=0, le=1)
    prob_points_ge_10: float | None = Field(default=None, ge=0, le=1)
    model_variant: str = Field(min_length=1)
    data_timestamp: AwareDatetime
    xpoints_run_id: str | None = None
    minutes_run_id: str | None = None
    decision_run_id: str | None = None


class ExactPlayerIdentityResolver:
    """Resolve only exact external references in one explicitly named namespace.

    The fingerprint covers the namespace and the full canonical mapping, including
    collisions. It therefore changes deterministically whenever identity inputs change.
    """

    def __init__(self, players: Sequence[Player], namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("identity namespace must not be blank")
        self.namespace = namespace
        identities: dict[str, list[UUID]] = {}
        fingerprint_rows: list[str] = []
        for player in players:
            for external_ref in player.external_refs:
                if external_ref.provider == namespace:
                    identities.setdefault(external_ref.external_id, []).append(player.id)
                    fingerprint_rows.append(f"{external_ref.external_id}\0{player.id}")
        self._identities = identities
        digest = hashlib.sha256()
        digest.update(namespace.encode())
        digest.update(b"\0")
        for row in sorted(fingerprint_rows):
            digest.update(row.encode())
            digest.update(b"\n")
        self.fingerprint = digest.hexdigest()

    def resolve(self, external_player_id: str, *, provider_id: str) -> UUID:
        matches = self._identities.get(external_player_id, [])
        if not matches:
            raise ProviderMappingError(
                f"unmapped player external ID {external_player_id!r} "
                f"in namespace {self.namespace!r}",
                provider_id=provider_id,
            )
        if len(matches) != 1:
            raise ProviderMappingError(
                f"ambiguous player external ID {external_player_id!r} "
                f"in namespace {self.namespace!r}",
                provider_id=provider_id,
            )
        return matches[0]


def _read_csv(path: Path, provider_id: str) -> tuple[bytes, list[dict[str, str | None]]]:
    """Read exact UTF-8 bytes once; empty CSV cells carry missing-value semantics."""

    try:
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
    except OSError as exc:
        raise ProviderDataError(
            f"cannot read local projection CSV: {path}", provider_id=provider_id
        ) from exc
    except UnicodeDecodeError as exc:
        raise ProviderDataError(
            "projection CSV must be UTF-8 encoded", provider_id=provider_id
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ProviderDataError("projection CSV has no header", provider_id=provider_id)
    rows: list[dict[str, str | None]] = []
    try:
        for row in reader:
            if None in row:
                raise ProviderDataError(
                    "projection CSV row has more values than header columns",
                    provider_id=provider_id,
                )
            rows.append(
                {key: value if value not in (None, "") else None for key, value in row.items()}
            )
    except csv.Error as exc:
        raise ProviderDataError("malformed projection CSV", provider_id=provider_id) from exc
    if not rows:
        raise ProviderDataError("projection CSV contains no data rows", provider_id=provider_id)
    return data, rows


def _validate_row[RowT: BaseModel](
    row_type: type[RowT], row: Mapping[str, str | None], provider_id: str, row_number: int
) -> RowT:
    try:
        return row_type.model_validate(row)
    except ValidationError as exc:
        raise ProviderDataError(
            f"invalid projection CSV row {row_number}: {exc}", provider_id=provider_id
        ) from exc


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class _LocalCsvProjectionProvider:
    """Load, map and validate one immutable in-memory view of a supplied CSV."""

    def __init__(
        self,
        *,
        path: Path,
        players: Sequence[Player],
        identity_namespace: str,
        observed_at: datetime,
        season: str,
        descriptor: ProviderDescriptor,
        mapper: Callable[[Mapping[str, str | None], ExactPlayerIdentityResolver, int], Projection],
    ) -> None:
        _require_aware(observed_at, "observed_at")
        if not season.strip():
            raise ValueError("season must not be blank")
        self._descriptor = descriptor
        self._observed_at = observed_at
        self._season = season
        self._path = path.resolve()
        resolver = ExactPlayerIdentityResolver(players, identity_namespace)
        data, rows = _read_csv(self._path, descriptor.provider_id)
        self._source_sha256 = hashlib.sha256(data).hexdigest()
        self._mapping_fingerprint = resolver.fingerprint
        projections: list[Projection] = []
        keys: set[tuple[UUID, int]] = set()
        for row_number, row in enumerate(rows, start=2):
            try:
                projection = mapper(row, resolver, row_number)
            except (ProviderDataError, ProviderMappingError):
                raise
            except ValidationError as exc:
                raise ProviderDataError(
                    f"invalid canonical projection at CSV row {row_number}: {exc}",
                    provider_id=descriptor.provider_id,
                ) from exc
            key = (projection.player_id, projection.gameweek.value)
            if key in keys:
                raise ProviderDataError(
                    f"duplicate projection for canonical player {projection.player_id} "
                    f"and gameweek {projection.gameweek.value}",
                    provider_id=descriptor.provider_id,
                )
            keys.add(key)
            projections.append(projection)
        self._projections = tuple(projections)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def source_sha256(self) -> str:
        return self._source_sha256

    @property
    def mapping_fingerprint(self) -> str:
        return self._mapping_fingerprint

    def projections(
        self, gameweeks: Sequence[GameweekNumber]
    ) -> ProviderResponse[tuple[Projection, ...]]:
        requested = {gameweek.value for gameweek in gameweeks}
        selected = tuple(
            projection for projection in self._projections if projection.gameweek.value in requested
        )
        generated_at = max(
            (projection.generated_at for projection in selected), default=self._observed_at
        )
        return ProviderResponse(
            data=selected,
            provenance=ProviderProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.version,
                retrieved_at=self._observed_at,
                source_reference=str(self._path),
                snapshot_id=f"sha256:{self._source_sha256}",
                source_sha256=self._source_sha256,
                mapping_fingerprint=self._mapping_fingerprint,
                season=self._season,
            ),
            freshness=Freshness(as_of=generated_at),
        )


class GenericLocalCsvProjectionProvider(_LocalCsvProjectionProvider):
    """Import the project-owned projection CSV with an explicit identity namespace."""

    def __init__(
        self,
        path: Path,
        players: Sequence[Player],
        *,
        identity_namespace: str,
        provider_id: str,
        provider_version: str,
        season: str,
        observed_at: datetime,
    ) -> None:
        capabilities = {
            ProviderCapability.PROJECTIONS,
            ProviderCapability.EXPECTED_MINUTES,
            ProviderCapability.APPEARANCE_PROBABILITY,
            ProviderCapability.START_PROBABILITY,
            ProviderCapability.POINT_DISTRIBUTION,
        }
        descriptor = ProviderDescriptor(
            provider_id=provider_id,
            display_name="Generic local CSV projections",
            version=provider_version,
            capabilities=frozenset(capabilities),
        )

        def mapper(
            raw: Mapping[str, str | None],
            resolver: ExactPlayerIdentityResolver,
            row_number: int,
        ) -> Projection:
            row = _validate_row(GenericProjectionRow, raw, provider_id, row_number)
            return Projection(
                player_id=resolver.resolve(row.external_player_id, provider_id=provider_id),
                gameweek=GameweekNumber(value=row.gameweek),
                expected_points=row.expected_points,
                expected_minutes=row.expected_minutes,
                appearance_probability=row.appearance_probability,
                start_probability=row.start_probability,
                variance=row.variance,
                p10=row.p10,
                p50=row.p50,
                p90=row.p90,
                source=row.source,
                model_version=row.model_version,
                generated_at=row.generated_at,
            )

        super().__init__(
            path=path,
            players=players,
            identity_namespace=identity_namespace,
            observed_at=observed_at,
            season=season,
            descriptor=descriptor,
            mapper=mapper,
        )


def _fpl_forecast_external_id(stable_player_id: str, provider_id: str) -> str:
    match = _FPL_FORECAST_ID.fullmatch(stable_player_id)
    if match is None:
        raise ProviderMappingError(
            f"unsupported FPL Forecast stable_player_id {stable_player_id!r}; "
            "expected player_code_<positive integer>",
            provider_id=provider_id,
        )
    return match.group(1)


def _fpl_forecast_lineage(row: FplForecastRow, provider_version: str) -> str:
    parts = [
        provider_version,
        f"schema={row.schema_version}",
        f"model={row.model_variant}",
    ]
    parts.extend(
        f"{name}={value}"
        for name, value in (
            ("xpoints_run", row.xpoints_run_id),
            ("minutes_run", row.minutes_run_id),
            ("decision_run", row.decision_run_id),
        )
        if value
    )
    return "|".join(parts)


class FplForecastCsvAdapter(_LocalCsvProjectionProvider):
    """Adapt a user-supplied phase9 FPL Forecast CSV without importing AGPL code."""

    def __init__(
        self,
        path: Path,
        players: Sequence[Player],
        *,
        season: str,
        observed_at: datetime,
        provider_version: str = FPL_FORECAST_SCHEMA_VERSION,
    ) -> None:
        descriptor = ProviderDescriptor(
            provider_id=FPL_FORECAST_PROVIDER_ID,
            display_name="FPL Forecast local CSV",
            version=provider_version,
            capabilities=frozenset(
                {
                    ProviderCapability.PROJECTIONS,
                    ProviderCapability.EXPECTED_MINUTES,
                    ProviderCapability.APPEARANCE_PROBABILITY,
                    ProviderCapability.START_PROBABILITY,
                    ProviderCapability.POINT_DISTRIBUTION,
                }
            ),
        )

        def mapper(
            raw: Mapping[str, str | None],
            resolver: ExactPlayerIdentityResolver,
            row_number: int,
        ) -> Projection:
            row = _validate_row(FplForecastRow, raw, descriptor.provider_id, row_number)
            if row.season != season:
                raise ProviderDataError(
                    f"FPL Forecast row {row_number} season {row.season!r} "
                    f"does not match configured season {season!r}",
                    provider_id=descriptor.provider_id,
                )
            external_id = _fpl_forecast_external_id(row.stable_player_id, descriptor.provider_id)
            return Projection(
                player_id=resolver.resolve(external_id, provider_id=descriptor.provider_id),
                gameweek=GameweekNumber(value=row.gameweek),
                expected_points=row.expected_points,
                expected_minutes=row.expected_minutes,
                appearance_probability=row.p_appearance,
                start_probability=row.p_start,
                variance=row.points_std**2 if row.points_std is not None else None,
                p10=row.points_p10,
                p50=row.points_p50,
                p90=row.points_p90,
                source=FPL_FORECAST_PROVIDER_ID,
                model_version=_fpl_forecast_lineage(row, provider_version),
                generated_at=row.data_timestamp,
            )

        super().__init__(
            path=path,
            players=players,
            identity_namespace=FPL_CODE_NAMESPACE,
            observed_at=observed_at,
            season=season,
            descriptor=descriptor,
            mapper=mapper,
        )


class ProjectionProviderKind(StrEnum):
    """Projection adapters selectable through application configuration."""

    GENERIC_CSV = "generic_csv"
    FPL_FORECAST_CSV = "fpl_forecast_csv"


@dataclass(frozen=True, slots=True)
class ProjectionProviderConfiguration:
    """Local-file provider selection without source-specific application branching."""

    kind: ProjectionProviderKind
    path: Path
    season: str
    observed_at: datetime
    provider_id: str | None = None
    provider_version: str | None = None
    identity_namespace: str | None = None


def create_projection_provider(
    configuration: ProjectionProviderConfiguration, players: Sequence[Player]
) -> ProjectionProvider:
    """Construct the configured local adapter and enforce explicit generic identity settings."""

    if configuration.kind is ProjectionProviderKind.GENERIC_CSV:
        missing = [
            name
            for name, value in (
                ("provider_id", configuration.provider_id),
                ("provider_version", configuration.provider_version),
                ("identity_namespace", configuration.identity_namespace),
            )
            if value is None
        ]
        if missing:
            raise ValueError("generic CSV configuration requires: " + ", ".join(missing))
        assert configuration.provider_id is not None
        assert configuration.provider_version is not None
        assert configuration.identity_namespace is not None
        return GenericLocalCsvProjectionProvider(
            configuration.path,
            players,
            identity_namespace=configuration.identity_namespace,
            provider_id=configuration.provider_id,
            provider_version=configuration.provider_version,
            season=configuration.season,
            observed_at=configuration.observed_at,
        )
    return FplForecastCsvAdapter(
        configuration.path,
        players,
        season=configuration.season,
        observed_at=configuration.observed_at,
        provider_version=configuration.provider_version or FPL_FORECAST_SCHEMA_VERSION,
    )
