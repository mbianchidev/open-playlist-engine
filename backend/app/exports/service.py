from __future__ import annotations

import asyncio
import io
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from zipfile import ZIP_DEFLATED, ZipFile

from app.core.adapter import AccessDenied, NotFound
from app.core.migration_state import track_selected
from app.core.models import Playlist, PlaylistSelection
from app.exports.models import (
    ExportFormat,
    ExportManifest,
    ExportManifestEntry,
    ExportWarning,
)
from app.exports.serializers import (
    FORMAT_SPECS,
    FilenameAllocator,
    JsonBundleWriter,
    portable_location,
    safe_filename,
    write_manifest,
    write_playlist,
)

_STREAM_CHUNK_SIZE = 64 * 1024


class ExportGenerationError(Exception):
    pass


@dataclass
class LoadedPlaylist:
    playlist: Playlist
    warnings: list[ExportWarning] = field(default_factory=list)
    status: Literal["ok", "warning", "error"] = "ok"


class PlaylistLoader(Protocol):
    async def load(self, playlist_id: str) -> LoadedPlaylist: ...


@dataclass
class ExportArtifact:
    path: Path
    filename: str
    media_type: str
    warnings: list[ExportWarning] = field(default_factory=list)

    async def stream(self, *, chunk_size: int = _STREAM_CHUNK_SIZE) -> AsyncIterator[bytes]:
        try:
            with self.path.open("rb") as export_file:
                while chunk := await asyncio.to_thread(export_file.read, chunk_size):
                    yield chunk
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass
class _PreparedPlaylist:
    playlist: Playlist
    warnings: list[ExportWarning]
    status: Literal["ok", "warning", "error"]


async def build_export_artifact(
    *,
    export_format: ExportFormat,
    source_provider: str,
    selection: PlaylistSelection,
    loader: PlaylistLoader,
    max_playlists: int,
) -> ExportArtifact:
    _validate_selection(selection, max_playlists=max_playlists)
    if len(selection.playlist_ids) == 1:
        return await _build_single_artifact(
            export_format=export_format,
            source_provider=source_provider,
            playlist_id=selection.playlist_ids[0],
            selection=selection,
            loader=loader,
        )
    return await _build_archive_artifact(
        export_format=export_format,
        source_provider=source_provider,
        selection=selection,
        loader=loader,
    )


async def _build_single_artifact(
    *,
    export_format: ExportFormat,
    source_provider: str,
    playlist_id: str,
    selection: PlaylistSelection,
    loader: PlaylistLoader,
) -> ExportArtifact:
    loaded = await loader.load(playlist_id)
    prepared = _prepare_playlist(
        loaded,
        playlist_id=playlist_id,
        wanted=set(selection.tracks.get(playlist_id) or []),
        source_provider=source_provider,
    )
    path = _temporary_path()
    try:
        with path.open("w", encoding="utf-8", newline="") as stream:
            write_playlist(
                stream,
                export_format,
                prepared.playlist,
                source_provider=source_provider,
                warnings=prepared.warnings,
            )
        spec = FORMAT_SPECS[export_format]
        filename = FilenameAllocator().allocate(
            prepared.playlist.name,
            extension=spec.extension,
            fallback=prepared.playlist.id or playlist_id,
        )
        return ExportArtifact(
            path=path,
            filename=filename,
            media_type=spec.media_type,
            warnings=prepared.warnings,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def _build_archive_artifact(
    *,
    export_format: ExportFormat,
    source_provider: str,
    selection: PlaylistSelection,
    loader: PlaylistLoader,
) -> ExportArtifact:
    path = _temporary_path()
    warnings: list[ExportWarning] = []
    entries: list[ExportManifestEntry] = []
    successful_playlists = 0
    try:
        with ZipFile(path, "w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
            if export_format is ExportFormat.JSON:
                successful_playlists = await _write_json_archive_entry(
                    archive,
                    source_provider=source_provider,
                    selection=selection,
                    loader=loader,
                    entries=entries,
                    warnings=warnings,
                )
            else:
                successful_playlists = await _write_playlist_archive_entries(
                    archive,
                    export_format=export_format,
                    source_provider=source_provider,
                    selection=selection,
                    loader=loader,
                    entries=entries,
                    warnings=warnings,
                )
            if successful_playlists == 0:
                raise ExportGenerationError("None of the selected playlists could be read")
            manifest = ExportManifest(
                format=export_format,
                source_provider=source_provider,
                playlist_count=len(selection.playlist_ids),
                entries=entries,
                warnings=warnings,
            )
            _write_zip_text(
                archive,
                "manifest.json",
                lambda stream: write_manifest(stream, manifest),
            )
        archive_name = (
            f"{safe_filename(source_provider, fallback='source')}-playlists-"
            f"{export_format.value}.zip"
        )
        return ExportArtifact(
            path=path,
            filename=archive_name,
            media_type="application/zip",
            warnings=warnings,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def _write_json_archive_entry(
    archive: ZipFile,
    *,
    source_provider: str,
    selection: PlaylistSelection,
    loader: PlaylistLoader,
    entries: list[ExportManifestEntry],
    warnings: list[ExportWarning],
) -> int:
    successful = 0
    with archive.open("open-playlist-bundle.json", "w", force_zip64=True) as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="", write_through=True)
        try:
            writer = JsonBundleWriter(stream, source_provider=source_provider)
            for playlist_id in selection.playlist_ids:
                prepared = await _load_archive_playlist(
                    loader,
                    playlist_id=playlist_id,
                    wanted=set(selection.tracks.get(playlist_id) or []),
                    source_provider=source_provider,
                )
                writer.write_playlist(prepared.playlist)
                warnings.extend(prepared.warnings)
                entries.append(
                    _manifest_entry(
                        prepared,
                        filename="open-playlist-bundle.json",
                        playlist_id=playlist_id,
                    )
                )
                if prepared.status != "error":
                    successful += 1
            writer.finish(warnings)
            stream.flush()
        finally:
            stream.detach()
    return successful


async def _write_playlist_archive_entries(
    archive: ZipFile,
    *,
    export_format: ExportFormat,
    source_provider: str,
    selection: PlaylistSelection,
    loader: PlaylistLoader,
    entries: list[ExportManifestEntry],
    warnings: list[ExportWarning],
) -> int:
    successful = 0
    spec = FORMAT_SPECS[export_format]
    filenames = FilenameAllocator()
    for playlist_id in selection.playlist_ids:
        prepared = await _load_archive_playlist(
            loader,
            playlist_id=playlist_id,
            wanted=set(selection.tracks.get(playlist_id) or []),
            source_provider=source_provider,
        )
        filename = filenames.allocate(
            prepared.playlist.name,
            extension=spec.extension,
            fallback=prepared.playlist.id or playlist_id,
        )
        _write_zip_text(
            archive,
            filename,
            lambda stream, prepared=prepared: write_playlist(
                stream,
                export_format,
                prepared.playlist,
                source_provider=source_provider,
                warnings=prepared.warnings,
            ),
        )
        warnings.extend(prepared.warnings)
        entries.append(
            _manifest_entry(prepared, filename=filename, playlist_id=playlist_id)
        )
        if prepared.status != "error":
            successful += 1
    return successful


async def _load_archive_playlist(
    loader: PlaylistLoader,
    *,
    playlist_id: str,
    wanted: set[str],
    source_provider: str,
) -> _PreparedPlaylist:
    try:
        loaded = await loader.load(playlist_id)
    except (AccessDenied, NotFound) as exc:
        loaded = LoadedPlaylist(
            playlist=Playlist(id=playlist_id, name=playlist_id),
            warnings=[
                ExportWarning(
                    code="playlist_read_failed",
                    message=str(exc),
                    playlist_id=playlist_id,
                )
            ],
            status="error",
        )
    return _prepare_playlist(
        loaded,
        playlist_id=playlist_id,
        wanted=wanted,
        source_provider=source_provider,
    )


def _prepare_playlist(
    loaded: LoadedPlaylist,
    *,
    playlist_id: str,
    wanted: set[str],
    source_provider: str,
) -> _PreparedPlaylist:
    original_tracks = loaded.playlist.tracks
    selected_tracks = [
        _with_unsupported_reason(track)
        for track in original_tracks
        if track_selected(track, wanted)
    ]
    playlist = loaded.playlist.model_copy(update={"tracks": selected_tracks})
    warnings = list(loaded.warnings)
    if wanted and len(selected_tracks) < len(original_tracks):
        warnings.append(
            ExportWarning(
                code="partial_selection",
                message=(
                    f"Exported {len(selected_tracks)} of {len(original_tracks)} tracks "
                    f"from {playlist.name}."
                ),
                playlist_id=playlist.id or playlist_id,
            )
        )
    if not selected_tracks:
        warnings.append(
            ExportWarning(
                code="empty_playlist",
                message=f"{playlist.name} contains no selected tracks.",
                playlist_id=playlist.id or playlist_id,
            )
        )
    unsupported_count = sum(
        1 for track in selected_tracks if not track.is_migratable
    )
    if unsupported_count:
        warnings.append(
            ExportWarning(
                code="unsupported_items",
                message=(
                    f"{playlist.name} contains {unsupported_count} unsupported "
                    f"playlist item{'s' if unsupported_count != 1 else ''}."
                ),
                playlist_id=playlist.id or playlist_id,
            )
        )
    missing_uri_count = sum(
        1 for track in selected_tracks if portable_location(track, source_provider) is None
    )
    if missing_uri_count:
        warnings.append(
            ExportWarning(
                code="missing_source_uri",
                message=(
                    f"{playlist.name} contains {missing_uri_count} item"
                    f"{'s' if missing_uri_count != 1 else ''} without a provider URI."
                ),
                playlist_id=playlist.id or playlist_id,
            )
        )
    status = loaded.status
    if status == "ok" and warnings:
        status = "warning"
    return _PreparedPlaylist(playlist=playlist, warnings=warnings, status=status)


def _with_unsupported_reason(track):
    if track.is_migratable or track.unsupported_reason:
        return track
    return track.model_copy(
        update={"unsupported_reason": f"Unsupported media type: {track.media_type.value}"}
    )


def _manifest_entry(
    prepared: _PreparedPlaylist,
    *,
    filename: str,
    playlist_id: str,
) -> ExportManifestEntry:
    return ExportManifestEntry(
        playlist_id=prepared.playlist.id or playlist_id,
        playlist_name=prepared.playlist.name,
        filename=filename,
        status=prepared.status,
        track_count=len(prepared.playlist.tracks),
        warning_codes=[warning.code for warning in prepared.warnings],
    )


def _write_zip_text(archive: ZipFile, filename: str, write) -> None:
    with archive.open(filename, "w", force_zip64=True) as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="", write_through=True)
        try:
            write(stream)
            stream.flush()
        finally:
            stream.detach()


def _validate_selection(selection: PlaylistSelection, *, max_playlists: int) -> None:
    if not selection.playlist_ids:
        raise ExportGenerationError("Select at least one playlist to export")
    if len(selection.playlist_ids) > max_playlists:
        raise ExportGenerationError(
            f"Select at most {max_playlists} playlists per export"
        )
    if len(set(selection.playlist_ids)) != len(selection.playlist_ids):
        raise ExportGenerationError("Playlist selection contains duplicate IDs")
    unknown_track_filters = set(selection.tracks) - set(selection.playlist_ids)
    if unknown_track_filters:
        raise ExportGenerationError(
            "Track filters reference playlists that are not selected"
        )


def _temporary_path() -> Path:
    descriptor, filename = tempfile.mkstemp(prefix="ope-export-", suffix=".tmp")
    os.close(descriptor)
    return Path(filename)
