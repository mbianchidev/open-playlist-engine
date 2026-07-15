"""The universal interchange model — the hub of the hub-and-spoke design.

Mirrors the open-playlist OpenAPI spec (Playlist/Track), vendored at
``openapi/open-playlist.yaml``, and adds the item-level fidelity fields the
design doc requires so lossy migrations can be reported instead of silently
dropping data.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MediaType(StrEnum):
    TRACK = "track"
    EPISODE = "episode"
    VIDEO = "video"
    LOCAL_FILE = "local_file"
    UNKNOWN = "unknown"


class PlaylistKind(StrEnum):
    STANDARD = "standard"
    LIKED_TRACKS = "liked_tracks"


class Credit(BaseModel):
    role: str
    name: str
    instrument: str | None = None
    uri: str | None = None


class Track(BaseModel):
    """A single playlist item in universal form.

    Only ``title`` and ``artist`` are strictly required; ``isrc`` is the primary
    cross-provider match key when present.
    """

    id: str | None = None
    title: str
    artist: str
    album: str | None = None
    duration_s: int | None = None  # spec's ``duration`` (seconds), named for clarity
    release_date: date | None = None
    release_year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    explicit: bool | None = None
    composer: str | None = None
    credits: list[Credit] = Field(default_factory=list)
    label: str | None = None
    isrc: str | None = None
    artwork_uri: str | None = None
    provider_uris: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    migration_status: str | None = None
    migrated_target_playlist_id: str | None = None
    migrated_target_uri: str | None = None

    # Fidelity / lossy-migration tracking
    position: int | None = None
    media_type: MediaType = MediaType.TRACK
    is_local: bool = False
    source_item_id: str | None = None
    added_at: datetime | None = None
    unsupported_reason: str | None = None

    @property
    def is_migratable(self) -> bool:
        return self.media_type is MediaType.TRACK and not self.is_local


class PlaylistSelection(BaseModel):
    playlist_ids: list[str] = Field(default_factory=list)
    tracks: dict[str, list[str]] = Field(default_factory=dict)


class PlaylistRef(BaseModel):
    """Lightweight handle returned while listing, before full read."""

    id: str
    name: str
    track_count: int | None = None
    owner_id: str | None = None
    collaborative: bool | None = None
    snapshot_id: str | None = None
    tracks_href: str | None = None
    migration_status: str | None = None
    migrated_track_count: int = 0
    remaining_track_count: int | None = None
    migration_note: str | None = None
    kind: PlaylistKind = PlaylistKind.STANDARD


class Playlist(BaseModel):
    id: str | None = None
    name: str
    description: str | None = None
    photo: str | None = None
    tracks: list[Track] = Field(default_factory=list)
    owner_id: str | None = None
    snapshot_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    kind: PlaylistKind = PlaylistKind.STANDARD
