"""Read and persist byte-for-byte immutable local source snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from fpl_decision_engine.ports import ProviderDataError

SCHEMA_VERSION = 1
REQUIRED_RESOURCES = ("bootstrap-static", "fixtures")
STORED_FILENAMES = {
    "bootstrap-static": "bootstrap-static.json",
    "fixtures": "fixtures.json",
}
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{12}$")


class _InputObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_name: str
    original_filename: str
    stored_filename: str | None = None
    sha256: str | None = None
    byte_size: int | None = Field(default=None, ge=0)


class _InputManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    snapshot_id: str | None = None
    provider_id: str = "fpl"
    season: str | None = None
    observed_at: AwareDatetime
    published_at: AwareDatetime | None = None
    code_revision: str | None = None
    source_objects: tuple[_InputObject, ...]


class SnapshotObject(BaseModel):
    """Manifest metadata for one exact stored source object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_name: str
    original_filename: str
    stored_filename: str
    sha256: str
    byte_size: int = Field(ge=0)


class SnapshotManifest(BaseModel):
    """Versioned metadata stored alongside immutable source bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    snapshot_id: str
    provider_id: str
    season: str
    observed_at: AwareDatetime
    imported_at: AwareDatetime
    processed_at: AwareDatetime
    published_at: AwareDatetime | None = None
    code_revision: str | None = None
    source_objects: tuple[SnapshotObject, ...]


@dataclass(frozen=True, slots=True)
class RawSourceObject:
    resource_name: str
    original_filename: str
    data: bytes
    sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    """Validated local bytes prepared for canonical mapping and later storage."""

    provider_id: str
    observed_at: datetime
    objects: tuple[RawSourceObject, ...]
    season: str | None = None
    requested_snapshot_id: str | None = None
    published_at: datetime | None = None
    code_revision: str | None = None

    def with_season(self, season: str) -> Self:
        return replace(self, season=season)

    def object_bytes(self, resource_name: str) -> bytes:
        for source_object in self.objects:
            if source_object.resource_name == resource_name:
                return source_object.data
        raise KeyError(resource_name)

    @property
    def content_hash(self) -> str:
        digest = hashlib.sha256()
        for source_object in sorted(self.objects, key=lambda item: item.resource_name):
            digest.update(source_object.resource_name.encode())
            digest.update(b"\0")
            digest.update(source_object.data)
            digest.update(b"\0")
        return digest.hexdigest()

    @property
    def expected_snapshot_id(self) -> str:
        timestamp = self.observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}_{self.content_hash[:12]}"


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    path: Path
    manifest: SnapshotManifest
    created: bool


def _error(message: str, provider_id: str = "snapshot") -> ProviderDataError:
    return ProviderDataError(message, provider_id=provider_id)


def _require_safe_component(value: str, field_name: str, provider_id: str) -> None:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise _error(f"{field_name} contains unsafe path characters", provider_id)


def _load_manifest(path: Path) -> _InputManifest:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except OSError as exc:
        raise _error(f"cannot read snapshot manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise _error(f"malformed JSON in snapshot manifest: {path}") from exc

    try:
        return _InputManifest.model_validate(value)
    except ValidationError as exc:
        raise _error(f"invalid snapshot manifest: {exc}") from exc


def _read_object(path: Path, resource: _InputObject, provider_id: str) -> RawSourceObject:
    filename = resource.stored_filename or resource.original_filename
    if Path(filename).name != filename:
        raise _error(f"resource filename must not contain a path: {filename}", provider_id)
    object_path = path.parent / filename
    try:
        data = object_path.read_bytes()
    except OSError as exc:
        raise _error(f"missing required source file: {object_path}", provider_id) from exc

    digest = hashlib.sha256(data).hexdigest()
    if resource.sha256 is not None and resource.sha256 != digest:
        raise _error(f"SHA-256 mismatch for source object: {filename}", provider_id)
    if resource.byte_size is not None and resource.byte_size != len(data):
        raise _error(f"byte-size mismatch for source object: {filename}", provider_id)
    return RawSourceObject(
        resource_name=resource.resource_name,
        original_filename=resource.original_filename,
        data=data,
        sha256=digest,
    )


def prepare_snapshot(input_path: Path) -> PreparedSnapshot:
    """Read a complete local directory or manifest without writing any output."""

    input_path = input_path.resolve()
    manifest_path = input_path / "manifest.json" if input_path.is_dir() else input_path

    if manifest_path.is_file():
        manifest = _load_manifest(manifest_path)
        if manifest.schema_version != SCHEMA_VERSION:
            raise _error(
                f"unsupported snapshot manifest schema version: {manifest.schema_version}",
                manifest.provider_id,
            )
        resources = {item.resource_name: item for item in manifest.source_objects}
        missing = set(REQUIRED_RESOURCES) - resources.keys()
        extras = resources.keys() - set(REQUIRED_RESOURCES)
        if missing:
            raise _error(
                f"incomplete snapshot; missing resources: {', '.join(sorted(missing))}",
                manifest.provider_id,
            )
        if extras:
            raise _error(
                f"unsupported snapshot resources: {', '.join(sorted(extras))}",
                manifest.provider_id,
            )
        if len(resources) != len(manifest.source_objects):
            raise _error("snapshot manifest contains duplicate resources", manifest.provider_id)
        manifest_objects = tuple(
            _read_object(manifest_path, resources[name], manifest.provider_id)
            for name in REQUIRED_RESOURCES
        )
        return PreparedSnapshot(
            provider_id=manifest.provider_id,
            season=manifest.season,
            observed_at=manifest.observed_at,
            requested_snapshot_id=manifest.snapshot_id,
            published_at=manifest.published_at,
            code_revision=manifest.code_revision,
            objects=manifest_objects,
        )

    if not input_path.is_dir():
        raise _error(f"snapshot input does not exist: {input_path}")

    objects: list[RawSourceObject] = []
    modified_times: list[float] = []
    for resource_name in REQUIRED_RESOURCES:
        filename = STORED_FILENAMES[resource_name]
        source_path = input_path / filename
        try:
            data = source_path.read_bytes()
            modified_times.append(source_path.stat().st_mtime)
        except OSError as exc:
            raise _error(f"missing required source file: {source_path}") from exc
        objects.append(
            RawSourceObject(
                resource_name=resource_name,
                original_filename=filename,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    observed_at = datetime.fromtimestamp(max(modified_times), tz=UTC)
    return PreparedSnapshot(
        provider_id="fpl",
        observed_at=observed_at,
        objects=tuple(objects),
    )


class SnapshotStore:
    """Atomically persist immutable snapshots below a local raw-data root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def store(self, snapshot: PreparedSnapshot, *, imported_at: datetime) -> StoredSnapshot:
        if snapshot.season is None:
            raise _error(
                "snapshot season must be validated before persistence", snapshot.provider_id
            )
        if imported_at.tzinfo is None or imported_at.utcoffset() is None:
            raise ValueError("imported_at must be timezone-aware")
        _require_safe_component(snapshot.provider_id, "provider_id", snapshot.provider_id)
        _require_safe_component(snapshot.season, "season", snapshot.provider_id)

        expected_id = snapshot.expected_snapshot_id
        snapshot_id = snapshot.requested_snapshot_id or expected_id
        if not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise _error("snapshot_id has an invalid format", snapshot.provider_id)

        target = self.root / snapshot.provider_id / snapshot.season / snapshot_id
        if target.exists():
            return self._existing(snapshot, target)
        if snapshot_id != expected_id:
            raise _error(
                "snapshot_id does not match observed timestamp and source content hash",
                snapshot.provider_id,
            )

        manifest = self._manifest(snapshot, imported_at, snapshot_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=target.parent))
        try:
            for source_object in snapshot.objects:
                stored_name = STORED_FILENAMES[source_object.resource_name]
                (temporary / stored_name).write_bytes(source_object.data)
            (temporary / "manifest.json").write_text(
                manifest.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.rename(temporary, target)
            except FileExistsError:
                return self._existing(snapshot, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return StoredSnapshot(path=target, manifest=manifest, created=True)

    def _existing(self, snapshot: PreparedSnapshot, target: Path) -> StoredSnapshot:
        manifest_path = target / "manifest.json"
        try:
            manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise _error(
                f"existing snapshot is incomplete or has invalid metadata: {target}",
                snapshot.provider_id,
            ) from exc

        expected = {item.resource_name: item.sha256 for item in snapshot.objects}
        actual = {item.resource_name: item.sha256 for item in manifest.source_objects}
        bytes_match = all(
            (target / STORED_FILENAMES[item.resource_name]).is_file()
            and hashlib.sha256(
                (target / STORED_FILENAMES[item.resource_name]).read_bytes()
            ).hexdigest()
            == item.sha256
            for item in snapshot.objects
        )
        if expected != actual or not bytes_match:
            raise _error(
                f"immutable snapshot conflict at {target}; existing evidence differs",
                snapshot.provider_id,
            )
        return StoredSnapshot(path=target, manifest=manifest, created=False)

    @staticmethod
    def _manifest(
        snapshot: PreparedSnapshot,
        imported_at: datetime,
        snapshot_id: str,
    ) -> SnapshotManifest:
        assert snapshot.season is not None
        objects = tuple(
            SnapshotObject(
                resource_name=item.resource_name,
                original_filename=item.original_filename,
                stored_filename=STORED_FILENAMES[item.resource_name],
                sha256=item.sha256,
                byte_size=item.byte_size,
            )
            for item in snapshot.objects
        )
        return SnapshotManifest(
            snapshot_id=snapshot_id,
            provider_id=snapshot.provider_id,
            season=snapshot.season,
            observed_at=snapshot.observed_at,
            imported_at=imported_at,
            processed_at=imported_at,
            published_at=snapshot.published_at,
            code_revision=snapshot.code_revision,
            source_objects=objects,
        )
