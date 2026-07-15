from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Playlist, Track
from app.db import models as orm
from app.exports.models import ExportWarning
from app.exports.service import LoadedPlaylist

PLAYLIST_METADATA_KEY = "playlist_metadata"


def store_playlist_snapshot(
    job: orm.MigrationJob,
    playlist: Playlist,
    *,
    playlist_id: str | None = None,
) -> None:
    snapshot_id = playlist_id or playlist.id
    if not snapshot_id:
        raise ValueError("Cannot store playlist metadata without a playlist ID")
    selection = dict(job.selection or {})
    metadata = dict(selection.get(PLAYLIST_METADATA_KEY) or {})
    metadata[snapshot_id] = playlist.model_dump(mode="json", exclude={"tracks"})
    selection[PLAYLIST_METADATA_KEY] = metadata
    job.selection = selection


class HistoryPlaylistLoader:
    def __init__(self, session: AsyncSession, job: orm.MigrationJob) -> None:
        self._session = session
        self._job = job

    async def load(self, playlist_id: str) -> LoadedPlaylist:
        statement = (
            select(orm.JobItem)
            .where(
                orm.JobItem.job_id == self._job.id,
                orm.JobItem.source_playlist_id == playlist_id,
            )
            .order_by(orm.JobItem.position.asc(), orm.JobItem.id.asc())
        )
        items = list((await self._session.execute(statement)).scalars())
        return history_playlist_from_items(self._job, playlist_id, items)


def history_playlist_from_items(
    job: orm.MigrationJob,
    playlist_id: str,
    items: list[orm.JobItem],
) -> LoadedPlaylist:
    ordered_items = sorted(items, key=lambda item: (item.position, item.id))
    warnings: list[ExportWarning] = []
    tracks: list[Track] = []
    invalid_track_metadata = False
    for item in ordered_items:
        try:
            tracks.append(Track.model_validate(item.source_metadata or {}))
        except ValidationError:
            invalid_track_metadata = True
            tracks.append(_fallback_track(item))
    if invalid_track_metadata:
        warnings.append(
            ExportWarning(
                code="historical_track_metadata_partial",
                message=(
                    "Some historical track metadata was incomplete; available migration "
                    "fields were exported instead."
                ),
                playlist_id=playlist_id,
            )
        )

    snapshot = _playlist_snapshot(job, playlist_id)
    if snapshot is None:
        playlist = Playlist(
            id=playlist_id,
            name=next(
                (
                    item.source_playlist_name
                    for item in ordered_items
                    if item.source_playlist_name
                ),
                playlist_id,
            ),
            tracks=tracks,
        )
        warnings.append(
            ExportWarning(
                code="historical_playlist_metadata_partial",
                message=(
                    "This migration predates playlist metadata snapshots; description, "
                    "artwork, ownership, and timestamps may be unavailable."
                ),
                playlist_id=playlist_id,
            )
        )
    else:
        try:
            playlist = Playlist.model_validate({**snapshot, "id": playlist_id, "tracks": tracks})
        except ValidationError:
            playlist = Playlist(
                id=playlist_id,
                name=str(snapshot.get("name") or playlist_id),
                tracks=tracks,
            )
            warnings.append(
                ExportWarning(
                    code="historical_playlist_metadata_partial",
                    message=(
                        "Stored playlist metadata was invalid; the playlist name and "
                        "available tracks were exported."
                    ),
                    playlist_id=playlist_id,
                )
            )
    return LoadedPlaylist(
        playlist=playlist,
        warnings=warnings,
        status="warning" if warnings else "ok",
    )


def _playlist_snapshot(job: orm.MigrationJob, playlist_id: str) -> dict[str, Any] | None:
    selection = job.selection or {}
    metadata = selection.get(PLAYLIST_METADATA_KEY)
    if not isinstance(metadata, dict):
        return None
    snapshot = metadata.get(playlist_id)
    return snapshot if isinstance(snapshot, dict) else None


def _fallback_track(item: orm.JobItem) -> Track:
    metadata = item.source_metadata if isinstance(item.source_metadata, dict) else {}
    provider_uris = metadata.get("provider_uris")
    return Track(
        id=_optional_string(metadata.get("id")),
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        release_year=item.release_year,
        explicit=item.explicit,
        isrc=item.isrc,
        provider_uris=provider_uris if isinstance(provider_uris, dict) else {},
        position=item.position,
        source_item_id=_optional_string(metadata.get("source_item_id")),
        unsupported_reason=item.reason,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

