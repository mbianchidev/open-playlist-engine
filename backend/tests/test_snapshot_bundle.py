from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from app.core.adapter import AuthKind, ProviderCredential, ProviderError
from app.core.models import Playlist, PlaylistKind, PlaylistRef, Track
from app.snapshots.bundle import (
    SnapshotIntegrityError,
    SnapshotPathError,
    SnapshotSource,
    SnapshotStorage,
    UnsupportedSnapshotVersion,
)


class StreamingAdapter:
    def __init__(
        self,
        playlists: dict[str, list[Track]],
        *,
        fail_after: dict[str, int] | None = None,
        names: dict[str, str] | None = None,
    ):
        self.playlists = playlists
        self.fail_after = fail_after or {}
        self.names = names or {}
        self.read_playlist_calls = 0
        self.iterated_tracks = 0

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        for playlist_id, tracks in self.playlists.items():
            yield PlaylistRef(
                id=playlist_id,
                name=self.names.get(playlist_id, f"Playlist {playlist_id}"),
                track_count=len(tracks),
                kind=(
                    PlaylistKind.LIKED_TRACKS
                    if playlist_id == "liked"
                    else PlaylistKind.STANDARD
                ),
            )

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        for index, track in enumerate(self.playlists[ref.id]):
            if index == self.fail_after.get(ref.id):
                raise ProviderError(f"read failed for {ref.id}")
            self.iterated_tracks += 1
            yield track

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        self.read_playlist_calls += 1
        raise AssertionError("snapshot creation must stream with iter_playlist_items")


def _credential() -> ProviderCredential:
    return ProviderCredential(
        account_id="account-secret-id",
        provider="spotify",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
        access_token="top-secret-access-token",
        refresh_token="top-secret-refresh-token",
        extra={"cookie": "top-secret-cookie"},
    )


async def _create_bundle(
    tmp_path: Path,
    adapter: StreamingAdapter,
    *,
    collection_ids: Sequence[str],
    snapshot_id: str = "11111111-1111-4111-8111-111111111111",
):
    storage = SnapshotStorage(tmp_path)
    return await storage.create_bundle(
        snapshot_id=snapshot_id,
        library_id="22222222-2222-4222-8222-222222222222",
        profile_name="My local backup",
        sources=[
            SnapshotSource(
                source_key="33333333-3333-4333-8333-333333333333",
                provider="spotify",
                account_label="Personal Spotify",
                adapter=adapter,
                credential=_credential(),
                collection_ids=list(collection_ids),
            )
        ],
    )


@pytest.mark.asyncio
async def test_bundle_creation_streams_tracks_and_removes_credentials(tmp_path: Path) -> None:
    tracks = [
        Track(
            id=str(index),
            title=f"Song {index}",
            artist="Artist",
            position=index,
            provider_uris={
                "spotify": (
                    f"https://open.spotify.com/track/{index}"
                    "?access_token=secret&si=share-id"
                )
            },
            artwork_uri="https://images.example/cover.jpg?token=secret&size=large",
            metadata={
                "spotify_album_id": "album-1",
                "spotify_popularity": 42,
                "spotify_preview_url": "https://audio.example/preview.mp3",
                "session_value": "top-secret-session",
                "authorization": "Bearer top-secret",
            },
        )
        for index in range(2_000)
    ]
    adapter = StreamingAdapter({"playlist": tracks})

    result = await _create_bundle(tmp_path, adapter, collection_ids=["playlist"])

    assert adapter.read_playlist_calls == 0
    assert adapter.iterated_tracks == 2_000
    assert result.manifest.status == "complete"
    assert result.manifest.counts.collections == 1
    assert result.manifest.counts.items == 2_000

    verified = result.storage.verify_archive(
        result.archive_name,
        expected_archive_sha256=result.archive_sha256,
    )
    playlist = result.storage.read_playlist(
        result.archive_name,
        verified.manifest.collections[0].id,
        expected_archive_sha256=result.archive_sha256,
    )
    first = playlist.tracks[0]
    assert first.metadata == {
        "spotify_album_id": "album-1",
        "spotify_popularity": 42,
    }
    assert first.provider_uris["spotify"] == "https://open.spotify.com/track/0?si=share-id"
    assert first.artwork_uri == "https://images.example/cover.jpg?size=large"

    with zipfile.ZipFile(result.archive_path) as archive:
        payload = b"".join(archive.read(name) for name in archive.namelist())
    assert b"top-secret" not in payload
    assert b"access_token" not in payload
    assert b"refresh_token" not in payload
    assert b"preview.mp3" not in payload
    assert b"account-secret-id" not in payload


@pytest.mark.asyncio
async def test_bundle_records_partial_collection_failures(tmp_path: Path) -> None:
    adapter = StreamingAdapter(
        {
            "broken": [
                Track(title="Captured", artist="Artist", position=0),
                Track(title="Missing", artist="Artist", position=1),
            ],
            "liked": [Track(title="Liked", artist="Artist", position=0)],
        },
        fail_after={"broken": 1},
    )

    result = await _create_bundle(
        tmp_path,
        adapter,
        collection_ids=["broken", "missing", "liked"],
    )

    assert result.manifest.status == "partial"
    assert result.manifest.counts.collections == 2
    assert result.manifest.counts.items == 2
    assert result.manifest.counts.failed_collections == 2
    by_source_id = {
        collection.source_collection_id: collection
        for collection in result.manifest.collections
    }
    assert by_source_id["broken"].complete is False
    assert by_source_id["broken"].item_count == 1
    assert by_source_id["broken"].error == "read failed for broken"
    assert by_source_id["liked"].kind is PlaylistKind.LIKED_TRACKS
    assert any(failure.collection_id == "missing" for failure in result.manifest.failures)

    verified = result.storage.verify_archive(result.archive_name)
    broken = result.storage.read_playlist(
        result.archive_name,
        by_source_id["broken"].id,
        expected_archive_sha256=verified.archive_sha256,
    )
    assert [track.title for track in broken.tracks] == ["Captured"]


@pytest.mark.asyncio
async def test_verify_rejects_newer_schema_version(tmp_path: Path) -> None:
    result = await _create_bundle(
        tmp_path,
        StreamingAdapter({"playlist": [Track(title="Song", artist="Artist")]}),
        collection_ids=["playlist"],
    )
    _rewrite_manifest(result.archive_path, {"schema_version": 999})

    with pytest.raises(UnsupportedSnapshotVersion, match="newer snapshot schema"):
        result.storage.verify_archive(result.archive_name)


@pytest.mark.asyncio
async def test_verify_rejects_corrupted_collection_checksum(tmp_path: Path) -> None:
    result = await _create_bundle(
        tmp_path,
        StreamingAdapter({"playlist": [Track(title="Song", artist="Artist")]}),
        collection_ids=["playlist"],
    )
    collection_path = result.manifest.collections[0].path
    with zipfile.ZipFile(result.archive_path) as archive:
        payload = archive.read(collection_path).replace(b'"Song"', b'"Tune"', 1)
    _rewrite_member(result.archive_path, collection_path, payload)

    with pytest.raises(SnapshotIntegrityError, match="checksum"):
        result.storage.verify_archive(result.archive_name)


@pytest.mark.asyncio
async def test_verify_rejects_unsafe_archive_members(tmp_path: Path) -> None:
    result = await _create_bundle(
        tmp_path,
        StreamingAdapter({"playlist": [Track(title="Song", artist="Artist")]}),
        collection_ids=["playlist"],
    )
    _rewrite_manifest(
        result.archive_path,
        {
            "collections": [
                {
                    **result.manifest.collections[0].model_dump(mode="json"),
                    "path": "../escape.jsonl",
                }
            ]
        },
        member_renames={result.manifest.collections[0].path: "../escape.jsonl"},
    )

    with pytest.raises(SnapshotPathError, match="unsafe archive member"):
        result.storage.verify_archive(result.archive_name)


@pytest.mark.asyncio
async def test_verify_rejects_undeclared_audio_or_binary_members(tmp_path: Path) -> None:
    result = await _create_bundle(
        tmp_path,
        StreamingAdapter({"playlist": [Track(title="Song", artist="Artist")]}),
        collection_ids=["playlist"],
    )
    with zipfile.ZipFile(result.archive_path, "a") as archive:
        archive.writestr("audio/song.mp3", b"not really audio")

    with pytest.raises(SnapshotIntegrityError, match="undeclared archive member"):
        result.storage.verify_archive(result.archive_name)


def test_storage_rejects_paths_outside_configured_root(tmp_path: Path) -> None:
    storage = SnapshotStorage(tmp_path)

    with pytest.raises(SnapshotPathError):
        storage.archive_path("../outside.opb")
    with pytest.raises(SnapshotPathError):
        storage.archive_path("/tmp/outside.opb")
    with pytest.raises(SnapshotPathError):
        storage.archive_path("nested/outside.opb")


@pytest.mark.asyncio
async def test_manifest_limit_is_independent_from_track_record_limit(tmp_path: Path) -> None:
    adapter = StreamingAdapter({f"playlist-{index}": [] for index in range(40)})
    storage = SnapshotStorage(
        tmp_path,
        max_manifest_bytes=1_048_576,
        max_record_bytes=1_024,
    )

    result = await storage.create_bundle(
        snapshot_id="11111111-1111-4111-8111-111111111111",
        library_id="22222222-2222-4222-8222-222222222222",
        profile_name="Large profile",
        sources=[
            SnapshotSource(
                source_key="33333333-3333-4333-8333-333333333333",
                provider="spotify",
                account_label="Personal Spotify",
                adapter=adapter,
                credential=_credential(),
                collection_ids=list(adapter.playlists),
            )
        ],
    )

    assert result.manifest.counts.collections == 40
    assert len(json.dumps(result.manifest.model_dump(mode="json"))) > 1_024


@pytest.mark.asyncio
async def test_diff_reports_added_removed_renamed_and_changed_collections(
    tmp_path: Path,
) -> None:
    base = await _create_bundle(
        tmp_path,
        StreamingAdapter(
            {
                "playlist": [Track(id="one", title="One", artist="Artist")],
                "removed": [Track(id="gone", title="Gone", artist="Artist")],
            },
            names={"playlist": "Old name", "removed": "Removed"},
        ),
        collection_ids=["playlist", "removed"],
    )
    compare = await _create_bundle(
        tmp_path,
        StreamingAdapter(
            {
                "playlist": [Track(id="two", title="Two", artist="Artist")],
                "added": [Track(id="new", title="New", artist="Artist")],
            },
            names={"playlist": "New name", "added": "Added"},
        ),
        collection_ids=["playlist", "added"],
        snapshot_id="44444444-4444-4444-8444-444444444444",
    )

    diff = base.storage.diff_archives(base.archive_name, compare.archive_name)

    assert [collection.name for collection in diff.added] == ["Added"]
    assert [collection.name for collection in diff.removed] == ["Removed"]
    assert [collection.name for collection in diff.renamed] == ["New name"]
    assert [collection.name for collection in diff.changed] == ["New name"]
    assert diff.items_added == 2
    assert diff.items_removed == 2


@pytest.mark.asyncio
async def test_verify_rejects_credential_fields_even_with_matching_checksums(
    tmp_path: Path,
) -> None:
    result = await _create_bundle(
        tmp_path,
        StreamingAdapter({"playlist": [Track(title="Song", artist="Artist")]}),
        collection_ids=["playlist"],
    )
    collection_path = result.manifest.collections[0].path

    def add_credential(record: dict) -> dict:
        if record.get("record_type") == "track":
            record["track"]["metadata"] = {"authorization": "Bearer imported-secret"}
        return record

    _rewrite_collection_records(result.archive_path, collection_path, add_credential)

    with pytest.raises(SnapshotIntegrityError, match="unsupported keys"):
        result.storage.verify_archive(result.archive_name)


def _rewrite_manifest(
    path: Path,
    updates: dict,
    *,
    member_renames: dict[str, str] | None = None,
) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest.update(updates)
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    members["manifest.json"] = manifest_bytes
    members["manifest.sha256"] = (
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n"
    ).encode()
    renames = member_renames or {}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, payload in members.items():
            archive.writestr(renames.get(name, name), payload)


def _rewrite_member(path: Path, member_name: str, payload: bytes) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[member_name] = payload
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, member_payload in members.items():
            archive.writestr(name, member_payload)


def _rewrite_collection_records(path: Path, member_name: str, transform) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    records = [transform(json.loads(line)) for line in members[member_name].splitlines()]
    payload = b"".join(
        json.dumps(
            record,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
        for record in records
    )
    members[member_name] = payload
    manifest = json.loads(members["manifest.json"])
    collection = next(item for item in manifest["collections"] if item["path"] == member_name)
    item_lines = [
        line
        for line, record in zip(payload.splitlines(keepends=True), records, strict=True)
        if record.get("record_type") == "track"
    ]
    collection["payload_bytes"] = len(payload)
    collection["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    collection["items_sha256"] = hashlib.sha256(b"".join(item_lines)).hexdigest()
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    members["manifest.json"] = manifest_bytes
    members["manifest.sha256"] = (
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n"
    ).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, member_payload in members.items():
            archive.writestr(name, member_payload)
