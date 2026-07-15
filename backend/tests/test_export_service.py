from __future__ import annotations

import csv
import io
import json
from zipfile import ZipFile

import pytest

from app.core.adapter import AccessDenied, AuthExpired, NotFound
from app.core.models import Playlist, PlaylistSelection, Track
from app.exports import service
from app.exports.models import ExportFormat
from app.exports.serializers import parse_open_playlist_bundle
from app.exports.service import (
    ExportGenerationError,
    LoadedPlaylist,
    build_export_artifact,
)


class FakeLoader:
    def __init__(self, results: dict[str, Playlist | LoadedPlaylist | Exception]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def load(self, playlist_id: str) -> LoadedPlaylist:
        self.calls.append(playlist_id)
        result = self.results[playlist_id]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, LoadedPlaylist):
            return result
        return LoadedPlaylist(playlist=result)


def _playlist(playlist_id: str, name: str, count: int = 2) -> Playlist:
    return Playlist(
        id=playlist_id,
        name=name,
        tracks=[
            Track(
                id=f"{playlist_id}-track-{index}",
                title=f"Song {index}",
                artist="Artist",
                provider_uris={"spotify": f"spotify:track:{playlist_id}{index}"},
                position=index,
            )
            for index in range(count)
        ],
    )


@pytest.mark.asyncio
async def test_single_export_returns_direct_file_and_stream_cleanup() -> None:
    loader = FakeLoader({"one": _playlist("one", "../../Road Trip")})

    artifact = await build_export_artifact(
        export_format=ExportFormat.CSV,
        source_provider="spotify",
        selection=PlaylistSelection(playlist_ids=["one"]),
        loader=loader,
        max_playlists=10,
    )

    assert artifact.filename == "Road-Trip.csv"
    assert artifact.media_type.startswith("text/csv")
    assert artifact.path.exists()
    stream = artifact.stream(chunk_size=16)
    assert await anext(stream)
    await stream.aclose()
    assert not artifact.path.exists()


@pytest.mark.asyncio
async def test_multi_export_creates_zip_manifest_and_collision_safe_files() -> None:
    loader = FakeLoader(
        {
            "one": _playlist("one", "Mix"),
            "two": _playlist("two", "mix"),
        }
    )

    artifact = await build_export_artifact(
        export_format=ExportFormat.CSV,
        source_provider="spotify",
        selection=PlaylistSelection(
            playlist_ids=["one", "two"],
            tracks={"one": ["one-track-1"]},
        ),
        loader=loader,
        max_playlists=10,
    )

    try:
        assert artifact.filename == "spotify-playlists-csv.zip"
        assert artifact.media_type == "application/zip"
        with ZipFile(artifact.path) as archive:
            assert archive.namelist() == ["Mix.csv", "mix-2.csv", "manifest.json"]
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["schema_version"] == 1
            assert [entry["playlist_id"] for entry in manifest["entries"]] == ["one", "two"]
            assert manifest["entries"][0]["warning_codes"] == ["partial_selection"]
            rows = list(
                csv.DictReader(
                    io.StringIO(archive.read("Mix.csv").decode("utf-8-sig"))
                )
            )
            assert [row["track_id"] for row in rows] == ["one-track-1"]
    finally:
        artifact.cleanup()


@pytest.mark.asyncio
async def test_multi_json_export_streams_one_bundle_and_keeps_empty_playlist() -> None:
    large = _playlist("large", "Large", count=2_000)
    empty = _playlist("empty", "Empty", count=0)
    loader = FakeLoader({"large": large, "empty": empty})

    artifact = await build_export_artifact(
        export_format=ExportFormat.JSON,
        source_provider="spotify",
        selection=PlaylistSelection(playlist_ids=["large", "empty"]),
        loader=loader,
        max_playlists=10,
    )

    try:
        with ZipFile(artifact.path) as archive:
            assert archive.namelist() == ["open-playlist-bundle.json", "manifest.json"]
            bundle = parse_open_playlist_bundle(archive.read("open-playlist-bundle.json"))
            assert [playlist.id for playlist in bundle.playlists] == ["large", "empty"]
            assert len(bundle.playlists[0].tracks) == 2_000
            assert bundle.playlists[1].tracks == []
            assert any(warning.code == "empty_playlist" for warning in bundle.warnings)
    finally:
        artifact.cleanup()


@pytest.mark.asyncio
async def test_multi_export_keeps_per_playlist_access_failure_as_warning() -> None:
    loader = FakeLoader(
        {
            "blocked": AccessDenied("Provider denied this playlist"),
            "ok": _playlist("ok", "Available"),
        }
    )

    artifact = await build_export_artifact(
        export_format=ExportFormat.M3U8,
        source_provider="spotify",
        selection=PlaylistSelection(playlist_ids=["blocked", "ok"]),
        loader=loader,
        max_playlists=10,
    )

    try:
        with ZipFile(artifact.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["entries"][0]["status"] == "error"
            assert manifest["entries"][0]["warning_codes"][0] == "playlist_read_failed"
            assert manifest["entries"][1]["status"] == "ok"
    finally:
        artifact.cleanup()


@pytest.mark.asyncio
async def test_all_per_playlist_failures_remove_temporary_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "export.tmp"
    monkeypatch.setattr(service, "_temporary_path", lambda: path)
    loader = FakeLoader(
        {
            "missing": NotFound("gone"),
            "blocked": AccessDenied("denied"),
        }
    )

    with pytest.raises(ExportGenerationError, match="could be read"):
        await build_export_artifact(
            export_format=ExportFormat.CSV,
            source_provider="spotify",
            selection=PlaylistSelection(playlist_ids=["missing", "blocked"]),
            loader=loader,
            max_playlists=10,
        )

    assert not path.exists()


@pytest.mark.asyncio
async def test_systemic_provider_error_aborts_remaining_reads_and_cleans_up(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "export.tmp"
    monkeypatch.setattr(service, "_temporary_path", lambda: path)
    loader = FakeLoader(
        {
            "one": _playlist("one", "One"),
            "expired": AuthExpired("Reconnect"),
            "three": _playlist("three", "Three"),
        }
    )

    with pytest.raises(AuthExpired, match="Reconnect"):
        await build_export_artifact(
            export_format=ExportFormat.XSPF,
            source_provider="spotify",
            selection=PlaylistSelection(playlist_ids=["one", "expired", "three"]),
            loader=loader,
            max_playlists=10,
        )

    assert loader.calls == ["one", "expired"]
    assert not path.exists()
