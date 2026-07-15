from __future__ import annotations

from app.core.models import MediaType, Playlist, PlaylistKind, Track
from app.db import models as orm
from app.exports.history import (
    history_playlist_from_items,
    store_playlist_snapshot,
)


def _job(*, selection: dict | None = None) -> orm.MigrationJob:
    return orm.MigrationJob(
        id="job",
        user_id="local",
        source_provider="spotify",
        target_provider="ytmusic",
        source_account_id="source",
        target_account_id="target",
        selection=selection or {"playlist_ids": ["playlist"], "tracks": {}},
        status="done",
    )


def _item(
    *,
    position: int,
    status: str,
    track: Track,
    source_playlist_name: str = "Road Trip",
) -> orm.JobItem:
    return orm.JobItem(
        id=f"item-{position}",
        job_id="job",
        source_playlist_id="playlist",
        source_playlist_name=source_playlist_name,
        position=position,
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_s=track.duration_s,
        release_year=track.release_year,
        explicit=track.explicit,
        isrc=track.isrc,
        source_metadata=track.model_dump(mode="json"),
        status=status,
        reason="provider could not migrate item" if status != "written" else None,
    )


def test_store_playlist_snapshot_preserves_playlist_metadata_without_tracks() -> None:
    job = _job()
    playlist = Playlist(
        id="playlist",
        name="Road Trip",
        description="Drive order",
        photo="https://images.example/playlist.jpg",
        owner_id="owner",
        kind=PlaylistKind.LIKED_TRACKS,
        tracks=[Track(title="Song", artist="Artist")],
    )

    store_playlist_snapshot(job, playlist)

    snapshot = job.selection["playlist_metadata"]["playlist"]
    assert snapshot["name"] == "Road Trip"
    assert snapshot["description"] == "Drive order"
    assert snapshot["kind"] == "liked_tracks"
    assert "tracks" not in snapshot


def test_history_reconstruction_preserves_order_and_all_item_statuses() -> None:
    job = _job(
        selection={
            "playlist_ids": ["playlist"],
            "tracks": {},
            "playlist_metadata": {
                "playlist": {
                    "id": "playlist",
                    "name": "Road Trip",
                    "description": "Drive order",
                    "kind": "standard",
                }
            },
        }
    )
    written = Track(
        id="written",
        title="Written",
        artist="Artist",
        position=0,
        provider_uris={"spotify": "spotify:track:written"},
    )
    skipped = Track(
        id="skipped",
        title="Episode",
        artist="Host",
        position=1,
        media_type=MediaType.EPISODE,
        unsupported_reason="Episodes are unsupported",
    )
    failed = Track(
        id="failed",
        title="Missing",
        artist="Artist",
        position=2,
    )
    items = [
        _item(position=0, status="written", track=written),
        _item(position=1, status="skipped", track=skipped),
        _item(position=2, status="failed", track=failed),
    ]

    loaded = history_playlist_from_items(job, "playlist", items)

    assert loaded.playlist.description == "Drive order"
    assert [track.id for track in loaded.playlist.tracks] == [
        "written",
        "skipped",
        "failed",
    ]
    assert loaded.playlist.tracks[1].unsupported_reason == "Episodes are unsupported"
    assert loaded.warnings == []


def test_history_reconstruction_warns_for_older_jobs_without_playlist_snapshot() -> None:
    job = _job()
    item = _item(
        position=0,
        status="written",
        track=Track(id="track", title="Song", artist="Artist", position=0),
    )

    loaded = history_playlist_from_items(job, "playlist", [item])

    assert loaded.playlist.name == "Road Trip"
    assert loaded.playlist.tracks[0].id == "track"
    assert [warning.code for warning in loaded.warnings] == [
        "historical_playlist_metadata_partial"
    ]


def test_history_reconstruction_uses_explicit_fallback_for_invalid_track_metadata() -> None:
    job = _job()
    item = _item(
        position=0,
        status="failed",
        track=Track(id="track", title="Song", artist="Artist", position=0),
    )
    item.source_metadata = {"title": None}

    loaded = history_playlist_from_items(job, "playlist", [item])

    assert loaded.playlist.tracks[0].title == "Song"
    assert loaded.playlist.tracks[0].unsupported_reason == "provider could not migrate item"
    assert [warning.code for warning in loaded.warnings] == [
        "historical_track_metadata_partial",
        "historical_playlist_metadata_partial",
    ]
