"""Versioned, credential-free Open Playlist snapshot bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import uuid
import zipfile
from collections import Counter
from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.adapter import ProviderAdapter, ProviderCredential, ProviderError
from app.core.migration_state import normalize_text
from app.core.models import Credit, Playlist, PlaylistKind, PlaylistRef, Track

FORMAT_NAME = "open-playlist-bundle"
SCHEMA_VERSION = 1
_ARCHIVE_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.opb$"
)
_SAFE_METADATA_KEYS = {
    "applemusic_catalog_id",
    "applemusic_genres",
    "applemusic_library_id",
    "applemusic_url",
    "spotify_album_id",
    "spotify_popularity",
    "tidal_media_tags",
    "tidal_popularity",
    "tidal_version",
}
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "jwt",
    "key",
    "private_key",
    "refresh_token",
    "session",
    "session_id",
    "sig",
    "signature",
    "token",
}
_MANIFEST_NAME = "manifest.json"
_MANIFEST_CHECKSUM_NAME = "manifest.sha256"
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(bearer\s+\S+|eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(access[_ -]?token|refresh[_ -]?token|authorization|cookie|password|secret|"
    r"private[_ -]?key|session[_ -]?id)\s*[:=]\s*\S+)"
)


class SnapshotError(Exception):
    """Base error for snapshot storage and format failures."""


class SnapshotPathError(SnapshotError):
    """A filesystem or archive member path escaped the configured boundary."""


class SnapshotIntegrityError(SnapshotError):
    """A bundle is malformed, unsafe, or does not match its checksums."""


class UnsupportedSnapshotVersion(SnapshotError):
    """A bundle uses a schema newer than this application understands."""


class SnapshotSourceManifest(BaseModel):
    key: str
    provider: str
    account_label: str | None = None
    selected_collection_count: int = 0

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _uuid_string(value, "snapshot source key")


class SnapshotFailure(BaseModel):
    source_key: str
    provider: str
    collection_id: str | None = None
    message: str


class SnapshotCounts(BaseModel):
    sources: int = 0
    collections: int = 0
    items: int = 0
    failed_collections: int = 0


class SnapshotPlaylistMetadata(BaseModel):
    id: str
    name: str
    owner_id: str | None = None
    snapshot_id: str | None = None
    kind: PlaylistKind = PlaylistKind.STANDARD


class SnapshotCollectionManifest(BaseModel):
    id: str
    source_key: str
    source_provider: str
    source_collection_id: str
    entity_type: Literal["playlist"] = "playlist"
    name: str
    kind: PlaylistKind = PlaylistKind.STANDARD
    path: str
    item_count: int = 0
    payload_bytes: int = 0
    payload_sha256: str
    items_sha256: str
    complete: bool = True
    error: str | None = None

    @field_validator("id", "source_key")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _uuid_string(value, "snapshot collection id")

    @model_validator(mode="after")
    def validate_completion(self) -> SnapshotCollectionManifest:
        if self.complete and self.error:
            raise ValueError("complete collections cannot include an error")
        if not self.complete and not self.error:
            raise ValueError("partial collections must include an error")
        return self


class SnapshotManifest(BaseModel):
    format: Literal["open-playlist-bundle"] = FORMAT_NAME
    schema_version: int = SCHEMA_VERSION
    snapshot_id: str
    library_id: str
    created_at: datetime
    profile_name: str | None = None
    status: Literal["complete", "partial"]
    sources: list[SnapshotSourceManifest] = Field(default_factory=list)
    counts: SnapshotCounts = Field(default_factory=SnapshotCounts)
    collections: list[SnapshotCollectionManifest] = Field(default_factory=list)
    failures: list[SnapshotFailure] = Field(default_factory=list)

    @field_validator("snapshot_id", "library_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _uuid_string(value, "snapshot manifest id")

    @model_validator(mode="after")
    def validate_consistency(self) -> SnapshotManifest:
        source_by_key = {source.key: source.provider for source in self.sources}
        if len(source_by_key) != len(self.sources):
            raise ValueError("snapshot manifest repeats a source key")
        collection_ids = {collection.id for collection in self.collections}
        if len(collection_ids) != len(self.collections):
            raise ValueError("snapshot manifest repeats a collection id")
        for collection in self.collections:
            if source_by_key.get(collection.source_key) != collection.source_provider:
                raise ValueError("snapshot collection source does not match the manifest")
        for failure in self.failures:
            if source_by_key.get(failure.source_key) != failure.provider:
                raise ValueError("snapshot failure source does not match the manifest")
        if self.counts.sources != len(self.sources):
            raise ValueError("snapshot source count does not match")
        if self.counts.collections != len(self.collections):
            raise ValueError("snapshot collection count does not match")
        if self.counts.items != sum(collection.item_count for collection in self.collections):
            raise ValueError("snapshot item count does not match")
        if self.counts.failed_collections != len(self.failures):
            raise ValueError("snapshot failure count does not match")
        if (self.status == "partial") != bool(self.failures):
            raise ValueError("snapshot status does not match its failures")
        text_values = [
            self.profile_name,
            *(source.account_label for source in self.sources),
            *(failure.message for failure in self.failures),
        ]
        if any(_credential_like(value) for value in text_values if value):
            raise ValueError("snapshot manifest contains credential-like text")
        return self


class SnapshotDiffCollection(BaseModel):
    id: str
    name: str
    previous_name: str | None = None
    item_count: int = 0
    previous_item_count: int | None = None


class SnapshotDiff(BaseModel):
    base_snapshot_id: str
    compare_snapshot_id: str
    added: list[SnapshotDiffCollection] = Field(default_factory=list)
    removed: list[SnapshotDiffCollection] = Field(default_factory=list)
    renamed: list[SnapshotDiffCollection] = Field(default_factory=list)
    changed: list[SnapshotDiffCollection] = Field(default_factory=list)
    items_added: int = 0
    items_removed: int = 0


@dataclass(slots=True)
class SnapshotSource:
    source_key: str
    provider: str
    account_label: str | None
    adapter: ProviderAdapter
    credential: ProviderCredential
    collection_ids: list[str]


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    manifest: SnapshotManifest
    archive_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BundleWriteResult:
    storage: SnapshotStorage
    archive_name: str
    archive_path: Path
    archive_sha256: str
    size_bytes: int
    manifest: SnapshotManifest


class SnapshotStorage:
    """Owns every filesystem operation below one configured snapshot root."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_archive_bytes: int = 1_073_741_824,
        max_uncompressed_bytes: int = 4_294_967_296,
        max_manifest_bytes: int = 67_108_864,
        max_record_bytes: int = 2_097_152,
        max_compression_ratio: int = 200,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_archive_bytes = max_archive_bytes
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.max_manifest_bytes = max_manifest_bytes
        self.max_record_bytes = max_record_bytes
        self.max_compression_ratio = max_compression_ratio
        self.root.mkdir(parents=True, exist_ok=True)

    def archive_path(self, archive_name: str) -> Path:
        if not _ARCHIVE_NAME.fullmatch(archive_name):
            raise SnapshotPathError("snapshot archive name is not a generated UUID basename")
        candidate = self.root / archive_name
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise SnapshotPathError("snapshot path escapes the configured snapshot directory")
        return candidate

    def create_temp_path(self, purpose: str) -> Path:
        safe_purpose = re.sub(r"[^a-z0-9-]+", "-", purpose.lower()).strip("-") or "snapshot"
        descriptor, raw_path = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{safe_purpose}-",
            suffix=".tmp",
        )
        os.close(descriptor)
        return Path(raw_path)

    async def write_upload(self, chunks: AsyncIterable[bytes]) -> tuple[Path, str, int]:
        path = self.create_temp_path("import")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("wb") as output:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_archive_bytes:
                        raise SnapshotIntegrityError(
                            f"snapshot archive exceeds {self.max_archive_bytes} bytes"
                        )
                    output.write(chunk)
                    digest.update(chunk)
            return path, digest.hexdigest(), size
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def adopt_temp_archive(self, temp_path: Path, archive_name: str) -> Path:
        final_path = self.archive_path(archive_name)
        if final_path.exists():
            raise SnapshotIntegrityError("snapshot archive already exists")
        os.replace(temp_path, final_path)
        return final_path

    def remove_archive(self, archive_name: str) -> None:
        path = self.archive_path(archive_name)
        path.unlink(missing_ok=True)

    async def create_bundle(
        self,
        *,
        snapshot_id: str,
        library_id: str,
        profile_name: str | None,
        sources: Sequence[SnapshotSource],
        created_at: datetime | None = None,
    ) -> BundleWriteResult:
        snapshot_uuid = str(uuid.UUID(snapshot_id))
        library_uuid = str(uuid.UUID(library_id))
        archive_name = f"{snapshot_uuid}.opb"
        final_path = self.archive_path(archive_name)
        if final_path.exists():
            raise SnapshotIntegrityError("snapshot archive already exists")

        created_at = created_at or datetime.now(UTC)
        temp_path = self.create_temp_path("create")
        failures: list[SnapshotFailure] = []
        collection_manifests: list[SnapshotCollectionManifest] = []
        source_manifests: list[SnapshotSourceManifest] = []
        item_count = 0
        failed_collection_count = 0
        try:
            with zipfile.ZipFile(
                temp_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                collection_index = 0
                for source in sources:
                    source_manifests.append(
                        SnapshotSourceManifest(
                            key=source.source_key,
                            provider=source.provider,
                            account_label=source.account_label,
                            selected_collection_count=len(source.collection_ids),
                        )
                    )
                    try:
                        refs = {
                            ref.id: ref
                            async for ref in source.adapter.iter_playlists(source.credential)
                            if ref.id in set(source.collection_ids)
                        }
                    except ProviderError as exc:
                        error = _safe_error_message(exc)
                        for collection_id in source.collection_ids:
                            failures.append(
                                SnapshotFailure(
                                    source_key=source.source_key,
                                    provider=source.provider,
                                    collection_id=collection_id,
                                    message=error,
                                )
                            )
                        failed_collection_count += len(source.collection_ids)
                        continue

                    for source_collection_id in source.collection_ids:
                        ref = refs.get(source_collection_id)
                        if ref is None:
                            failures.append(
                                SnapshotFailure(
                                    source_key=source.source_key,
                                    provider=source.provider,
                                    collection_id=source_collection_id,
                                    message="selected collection was not returned by the provider",
                                )
                            )
                            failed_collection_count += 1
                            continue
                        collection_index += 1
                        collection_id = _collection_id(
                            library_uuid,
                            source.source_key,
                            source.provider,
                            source_collection_id,
                        )
                        collection_path = (
                            f"collections/{collection_index:04d}-{collection_id}.jsonl"
                        )
                        (
                            collection_manifest,
                            collection_items,
                        ) = await self._write_collection(
                            archive,
                            path=collection_path,
                            collection_id=collection_id,
                            source=source,
                            ref=ref,
                        )
                        collection_manifests.append(collection_manifest)
                        item_count += collection_items
                        if not collection_manifest.complete:
                            failed_collection_count += 1
                            failures.append(
                                SnapshotFailure(
                                    source_key=source.source_key,
                                    provider=source.provider,
                                    collection_id=source_collection_id,
                                    message=(
                                        collection_manifest.error or "provider read failed"
                                    ),
                                )
                            )

                manifest = SnapshotManifest(
                    snapshot_id=snapshot_uuid,
                    library_id=library_uuid,
                    created_at=created_at,
                    profile_name=profile_name,
                    status="partial" if failures else "complete",
                    sources=source_manifests,
                    counts=SnapshotCounts(
                        sources=len(source_manifests),
                        collections=len(collection_manifests),
                        items=item_count,
                        failed_collections=failed_collection_count,
                    ),
                    collections=collection_manifests,
                    failures=failures,
                )
                manifest_bytes = _canonical_json(manifest.model_dump(mode="json"))
                if len(manifest_bytes) > self.max_manifest_bytes:
                    raise SnapshotIntegrityError(
                        f"snapshot manifest exceeds {self.max_manifest_bytes} bytes"
                    )
                archive.writestr(_MANIFEST_NAME, manifest_bytes)
                archive.writestr(
                    _MANIFEST_CHECKSUM_NAME,
                    (
                        f"{hashlib.sha256(manifest_bytes).hexdigest()}  {_MANIFEST_NAME}\n"
                    ).encode(),
                )

            verified = self._verify_path(temp_path)
            os.replace(temp_path, final_path)
            return BundleWriteResult(
                storage=self,
                archive_name=archive_name,
                archive_path=final_path,
                archive_sha256=verified.archive_sha256,
                size_bytes=verified.size_bytes,
                manifest=verified.manifest,
            )
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    async def _write_collection(
        self,
        archive: zipfile.ZipFile,
        *,
        path: str,
        collection_id: str,
        source: SnapshotSource,
        ref: PlaylistRef,
    ) -> tuple[SnapshotCollectionManifest, int]:
        payload_digest = hashlib.sha256()
        items_digest = hashlib.sha256()
        payload_bytes = 0
        item_count = 0
        complete = True
        error: str | None = None
        header = {
            "record_type": "collection",
            "playlist": SnapshotPlaylistMetadata(
                id=collection_id,
                name=ref.name,
                owner_id=ref.owner_id,
                snapshot_id=ref.snapshot_id,
                kind=ref.kind,
            ).model_dump(mode="json"),
        }
        info = zipfile.ZipInfo(path)
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=True) as output:
            header_line = _json_line(header)
            output.write(header_line)
            payload_digest.update(header_line)
            payload_bytes += len(header_line)
            try:
                async for track in source.adapter.iter_playlist_items(source.credential, ref):
                    safe_track = _snapshot_track(track)
                    track_line = _json_line(
                        {
                            "record_type": "track",
                            "track": safe_track.model_dump(mode="json"),
                        }
                    )
                    if len(track_line) > self.max_record_bytes:
                        raise SnapshotIntegrityError(
                            f"snapshot record exceeds {self.max_record_bytes} bytes"
                        )
                    output.write(track_line)
                    payload_digest.update(track_line)
                    items_digest.update(track_line)
                    payload_bytes += len(track_line)
                    item_count += 1
            except ProviderError as exc:
                complete = False
                error = _safe_error_message(exc)

        return (
            SnapshotCollectionManifest(
                id=collection_id,
                source_key=source.source_key,
                source_provider=source.provider,
                source_collection_id=ref.id,
                name=ref.name,
                kind=ref.kind,
                path=path,
                item_count=item_count,
                payload_bytes=payload_bytes,
                payload_sha256=payload_digest.hexdigest(),
                items_sha256=items_digest.hexdigest(),
                complete=complete,
                error=error,
            ),
            item_count,
        )

    def verify_archive(
        self,
        archive_name: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> VerifiedSnapshot:
        return self._verify_path(
            self.archive_path(archive_name),
            expected_archive_sha256=expected_archive_sha256,
        )

    def verify_temp_archive(
        self,
        temp_path: Path,
        *,
        expected_archive_sha256: str | None = None,
    ) -> VerifiedSnapshot:
        resolved = temp_path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise SnapshotPathError("temporary snapshot path escapes the configured directory")
        return self._verify_path(
            resolved,
            expected_archive_sha256=expected_archive_sha256,
        )

    def _verify_path(
        self,
        path: Path,
        *,
        expected_archive_sha256: str | None = None,
    ) -> VerifiedSnapshot:
        archive_sha256, archive_size = _file_digest(path)
        if archive_size > self.max_archive_bytes:
            raise SnapshotIntegrityError(
                f"snapshot archive exceeds {self.max_archive_bytes} bytes"
            )
        if expected_archive_sha256 and archive_sha256 != expected_archive_sha256:
            raise SnapshotIntegrityError("snapshot archive checksum does not match")
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise SnapshotIntegrityError("snapshot archive contains duplicate members")
                for info in infos:
                    _validate_member_name(info.filename)
                    if info.flag_bits & 0x1:
                        raise SnapshotIntegrityError("encrypted snapshot members are not supported")
                    if (
                        info.file_size > 0
                        and info.file_size / max(1, info.compress_size)
                        > self.max_compression_ratio
                    ):
                        raise SnapshotIntegrityError(
                            f"snapshot member compression ratio is unsafe: {info.filename}"
                        )

                manifest_bytes = self._read_small_member(
                    archive,
                    _MANIFEST_NAME,
                    limit=self.max_manifest_bytes,
                )
                checksum_bytes = self._read_small_member(
                    archive,
                    _MANIFEST_CHECKSUM_NAME,
                    limit=1_024,
                )
                expected_manifest_line = (
                    f"{hashlib.sha256(manifest_bytes).hexdigest()}  {_MANIFEST_NAME}\n"
                ).encode()
                if checksum_bytes != expected_manifest_line:
                    raise SnapshotIntegrityError("snapshot manifest checksum does not match")
                try:
                    raw_manifest = json.loads(manifest_bytes)
                except json.JSONDecodeError as exc:
                    raise SnapshotIntegrityError("snapshot manifest is not valid JSON") from exc
                schema_version = raw_manifest.get("schema_version")
                if not isinstance(schema_version, int):
                    raise SnapshotIntegrityError("snapshot manifest schema_version is invalid")
                if schema_version > SCHEMA_VERSION:
                    raise UnsupportedSnapshotVersion(
                        f"newer snapshot schema {schema_version} is not supported"
                    )
                if schema_version != SCHEMA_VERSION:
                    raise SnapshotIntegrityError(
                        f"snapshot schema {schema_version} is not supported"
                    )
                try:
                    manifest = SnapshotManifest.model_validate(raw_manifest)
                except ValueError as exc:
                    raise SnapshotIntegrityError(f"snapshot manifest is invalid: {exc}") from exc

                declared_paths = [collection.path for collection in manifest.collections]
                if len(declared_paths) != len(set(declared_paths)):
                    raise SnapshotIntegrityError("snapshot manifest repeats a collection path")
                for collection_path in declared_paths:
                    _validate_collection_member_name(collection_path)
                expected_members = {
                    _MANIFEST_NAME,
                    _MANIFEST_CHECKSUM_NAME,
                    *declared_paths,
                }
                undeclared = set(names) - expected_members
                missing = expected_members - set(names)
                if undeclared:
                    raise SnapshotIntegrityError(
                        f"undeclared archive member: {sorted(undeclared)[0]}"
                    )
                if missing:
                    raise SnapshotIntegrityError(
                        f"snapshot archive is missing member: {sorted(missing)[0]}"
                    )

                total_uncompressed = len(manifest_bytes) + len(checksum_bytes)
                for collection in manifest.collections:
                    consumed, _ = self._read_collection(
                        archive,
                        collection,
                        collect_tracks=False,
                    )
                    total_uncompressed += consumed
                    if total_uncompressed > self.max_uncompressed_bytes:
                        raise SnapshotIntegrityError(
                            "snapshot uncompressed content exceeds the configured limit"
                        )
        except zipfile.BadZipFile as exc:
            raise SnapshotIntegrityError("snapshot archive is not a valid ZIP bundle") from exc
        return VerifiedSnapshot(
            manifest=manifest,
            archive_sha256=archive_sha256,
            size_bytes=archive_size,
        )

    def _read_small_member(
        self,
        archive: zipfile.ZipFile,
        name: str,
        *,
        limit: int,
    ) -> bytes:
        try:
            info = archive.getinfo(name)
        except KeyError as exc:
            raise SnapshotIntegrityError(f"snapshot archive is missing {name}") from exc
        limit = min(limit, self.max_uncompressed_bytes)
        with archive.open(info) as member:
            payload = member.read(limit + 1)
        if len(payload) > limit:
            raise SnapshotIntegrityError(f"snapshot member is too large: {name}")
        return payload

    def _read_collection(
        self,
        archive: zipfile.ZipFile,
        collection: SnapshotCollectionManifest,
        *,
        collect_tracks: bool,
    ) -> tuple[int, Playlist | None]:
        try:
            member = archive.open(collection.path)
        except KeyError as exc:
            raise SnapshotIntegrityError(
                f"snapshot collection is missing: {collection.path}"
            ) from exc

        payload_digest = hashlib.sha256()
        items_digest = hashlib.sha256()
        payload_bytes = 0
        item_count = 0
        playlist_metadata: SnapshotPlaylistMetadata | None = None
        tracks: list[Track] = []
        with member:
            while True:
                line = member.readline(self.max_record_bytes + 1)
                if not line:
                    break
                if len(line) > self.max_record_bytes:
                    raise SnapshotIntegrityError(
                        f"snapshot record is too large: {collection.path}"
                    )
                payload_bytes += len(line)
                if payload_bytes > self.max_uncompressed_bytes:
                    raise SnapshotIntegrityError(
                        f"snapshot collection is too large: {collection.path}"
                    )
                payload_digest.update(line)
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SnapshotIntegrityError(
                        f"snapshot collection contains invalid JSON: {collection.path}"
                    ) from exc
                if playlist_metadata is None:
                    if record.get("record_type") != "collection":
                        raise SnapshotIntegrityError(
                            f"snapshot collection header is missing: {collection.path}"
                        )
                    try:
                        playlist_metadata = SnapshotPlaylistMetadata.model_validate(
                            record.get("playlist")
                        )
                    except ValueError as exc:
                        raise SnapshotIntegrityError(
                            f"snapshot collection header is invalid: {collection.path}"
                        ) from exc
                    continue
                if record.get("record_type") != "track":
                    raise SnapshotIntegrityError(
                        f"snapshot collection record is invalid: {collection.path}"
                    )
                try:
                    track = Track.model_validate(record.get("track"))
                except ValueError as exc:
                    raise SnapshotIntegrityError(
                        f"snapshot track is invalid: {collection.path}"
                    ) from exc
                _assert_snapshot_track_safe(track)
                items_digest.update(line)
                item_count += 1
                if collect_tracks:
                    tracks.append(track)

        if playlist_metadata is None:
            raise SnapshotIntegrityError(f"snapshot collection is empty: {collection.path}")
        if playlist_metadata.id != collection.id:
            raise SnapshotIntegrityError("snapshot collection id does not match its manifest")
        if playlist_metadata.name != collection.name:
            raise SnapshotIntegrityError("snapshot collection name does not match its manifest")
        if playlist_metadata.kind is not collection.kind:
            raise SnapshotIntegrityError("snapshot collection kind does not match its manifest")
        if payload_bytes != collection.payload_bytes:
            raise SnapshotIntegrityError("snapshot collection byte count does not match")
        if payload_digest.hexdigest() != collection.payload_sha256:
            raise SnapshotIntegrityError("snapshot collection checksum does not match")
        if items_digest.hexdigest() != collection.items_sha256:
            raise SnapshotIntegrityError("snapshot item checksum does not match")
        if item_count != collection.item_count:
            raise SnapshotIntegrityError("snapshot collection item count does not match")

        playlist = None
        if collect_tracks:
            playlist = Playlist(
                id=playlist_metadata.id,
                name=playlist_metadata.name,
                owner_id=playlist_metadata.owner_id,
                snapshot_id=playlist_metadata.snapshot_id,
                kind=playlist_metadata.kind,
                tracks=tracks,
            )
        return payload_bytes, playlist

    def read_playlist(
        self,
        archive_name: str,
        collection_id: str,
        *,
        expected_archive_sha256: str | None = None,
    ) -> Playlist:
        verified = self.verify_archive(
            archive_name,
            expected_archive_sha256=expected_archive_sha256,
        )
        return self.read_verified_playlist(
            archive_name,
            verified.manifest,
            collection_id,
        )

    def read_verified_playlist(
        self,
        archive_name: str,
        manifest: SnapshotManifest,
        collection_id: str,
    ) -> Playlist:
        collection = next(
            (
                candidate
                for candidate in manifest.collections
                if candidate.id == collection_id
            ),
            None,
        )
        if collection is None:
            raise SnapshotIntegrityError("snapshot collection was not found")
        with zipfile.ZipFile(self.archive_path(archive_name)) as archive:
            _, playlist = self._read_collection(
                archive,
                collection,
                collect_tracks=True,
            )
        if playlist is None:
            raise SnapshotIntegrityError("snapshot collection could not be read")
        return playlist

    def diff_archives(
        self,
        base_archive_name: str,
        compare_archive_name: str,
    ) -> SnapshotDiff:
        base = self.verify_archive(base_archive_name)
        compare = self.verify_archive(compare_archive_name)
        base_collections = {collection.id: collection for collection in base.manifest.collections}
        compare_collections = {
            collection.id: collection for collection in compare.manifest.collections
        }
        added_ids = sorted(compare_collections.keys() - base_collections.keys())
        removed_ids = sorted(base_collections.keys() - compare_collections.keys())
        common_ids = sorted(base_collections.keys() & compare_collections.keys())
        added = [
            _diff_collection(compare_collections[collection_id])
            for collection_id in added_ids
        ]
        removed = [
            _diff_collection(base_collections[collection_id])
            for collection_id in removed_ids
        ]
        renamed: list[SnapshotDiffCollection] = []
        changed: list[SnapshotDiffCollection] = []
        items_added = sum(collection.item_count for collection in added)
        items_removed = sum(collection.item_count for collection in removed)

        base_path = self.archive_path(base_archive_name)
        compare_path = self.archive_path(compare_archive_name)
        with zipfile.ZipFile(base_path) as base_archive, zipfile.ZipFile(
            compare_path
        ) as compare_archive:
            for collection_id in common_ids:
                before = base_collections[collection_id]
                after = compare_collections[collection_id]
                if before.name != after.name:
                    renamed.append(
                        _diff_collection(
                            after,
                            previous_name=before.name,
                            previous_item_count=before.item_count,
                        )
                    )
                if before.kind is not after.kind or before.items_sha256 != after.items_sha256:
                    changed.append(
                        _diff_collection(
                            after,
                            previous_name=before.name,
                            previous_item_count=before.item_count,
                        )
                    )
                    before_keys = self._collection_track_counts(base_archive, before)
                    after_keys = self._collection_track_counts(compare_archive, after)
                    items_added += sum((after_keys - before_keys).values())
                    items_removed += sum((before_keys - after_keys).values())

        return SnapshotDiff(
            base_snapshot_id=base.manifest.snapshot_id,
            compare_snapshot_id=compare.manifest.snapshot_id,
            added=added,
            removed=removed,
            renamed=renamed,
            changed=changed,
            items_added=items_added,
            items_removed=items_removed,
        )

    def _collection_track_counts(
        self,
        archive: zipfile.ZipFile,
        collection: SnapshotCollectionManifest,
    ) -> Counter[str]:
        _, playlist = self._read_collection(archive, collection, collect_tracks=True)
        if playlist is None:
            return Counter()
        return Counter(_track_identity(track) for track in playlist.tracks)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _json_line(payload: Any) -> bytes:
    return _canonical_json(payload) + b"\n"


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _collection_id(
    library_id: str,
    source_key: str,
    provider: str,
    source_collection_id: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"open-playlist:{library_id}:{source_key}:{provider}:{source_collection_id}",
        )
    )


def _snapshot_track(track: Track) -> Track:
    metadata = {
        key: value
        for key, value in track.metadata.items()
        if key in _SAFE_METADATA_KEYS
        and not _credential_like(value)
    }
    provider_uris = {
        provider: _sanitize_uri(uri)
        for provider, uri in track.provider_uris.items()
        if _sanitize_uri(uri)
    }
    credits = [
        Credit(
            role=credit.role,
            name=credit.name,
            instrument=credit.instrument,
            uri=_sanitize_uri(credit.uri),
        )
        for credit in track.credits
    ]
    return track.model_copy(
        update={
            "metadata": metadata,
            "provider_uris": provider_uris,
            "artwork_uri": _sanitize_uri(track.artwork_uri),
            "credits": credits,
        }
    )


def _assert_snapshot_track_safe(track: Track) -> None:
    unexpected_keys = set(track.metadata) - _SAFE_METADATA_KEYS
    if unexpected_keys:
        raise SnapshotIntegrityError(
            f"snapshot track metadata contains unsupported keys: {sorted(unexpected_keys)[0]}"
        )
    if _credential_like(track.metadata):
        raise SnapshotIntegrityError("snapshot track metadata contains credential-like data")
    uris = [
        track.artwork_uri,
        *track.provider_uris.values(),
        *(credit.uri for credit in track.credits),
    ]
    for uri in uris:
        if uri and _sanitize_uri(uri) != uri:
            raise SnapshotIntegrityError("snapshot track URI contains sensitive query data")


def _sanitize_uri(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme:
        return value
    host = parsed.netloc
    if parsed.netloc:
        hostname = parsed.hostname or ""
        if not hostname:
            return None
        host = f"{hostname}:{parsed.port}" if parsed.port else hostname
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [
        (key, item)
        for key, item in query
        if key.lower().replace("-", "_") not in _SENSITIVE_QUERY_KEYS
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            host,
            parsed.path,
            urllib.parse.urlencode(safe_query),
            parsed.fragment,
        )
    )


def _validate_member_name(name: str) -> None:
    if "\\" in name:
        raise SnapshotPathError(f"unsafe archive member path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SnapshotPathError(f"unsafe archive member path: {name}")
    if name.endswith("/"):
        raise SnapshotPathError(f"unsafe archive member path: {name}")


def _validate_collection_member_name(name: str) -> None:
    _validate_member_name(name)
    path = PurePosixPath(name)
    if len(path.parts) != 2 or path.parts[0] != "collections" or path.suffix != ".jsonl":
        raise SnapshotPathError(f"unsafe archive member path: {name}")


def _diff_collection(
    collection: SnapshotCollectionManifest,
    *,
    previous_name: str | None = None,
    previous_item_count: int | None = None,
) -> SnapshotDiffCollection:
    return SnapshotDiffCollection(
        id=collection.id,
        name=collection.name,
        previous_name=previous_name,
        item_count=collection.item_count,
        previous_item_count=previous_item_count,
    )


def _track_identity(track: Track) -> str:
    if track.isrc:
        return f"isrc:{track.isrc.upper()}"
    if track.provider_uris:
        provider, uri = sorted(track.provider_uris.items())[0]
        return f"uri:{provider}:{uri.lower()}"
    if track.source_item_id or track.id:
        return f"id:{(track.source_item_id or track.id or '').lower()}"
    return (
        f"song:{normalize_text(track.title)}|{normalize_text(track.artist)}|"
        f"{normalize_text(track.album)}|{round((track.duration_s or 0) / 5) * 5}"
    )


def _uuid_string(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _credential_like(value: Any) -> bool:
    if isinstance(value, str):
        if len(value) > 4_096:
            return True
        return bool(_CREDENTIAL_TEXT.search(value))
    if isinstance(value, dict):
        return any(
            key.lower().replace("-", "_") in _SENSITIVE_QUERY_KEYS
            or _credential_like(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_credential_like(item) for item in value)
    return False


def _safe_error_message(exc: ProviderError) -> str:
    message = str(exc).strip() or "provider read failed"
    if _credential_like(message):
        return "provider read failed; reconnect the account or retry"
    return message[:500]
