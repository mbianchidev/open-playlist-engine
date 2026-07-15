from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import Playlist

OPEN_PLAYLIST_BUNDLE_SCHEMA = (
    "https://openplaylistengine.dev/schemas/export/open-playlist-bundle-v1.json"
)
EXPORT_MANIFEST_SCHEMA = (
    "https://openplaylistengine.dev/schemas/export/open-playlist-export-manifest-v1.json"
)


class ExportFormat(StrEnum):
    CSV = "csv"
    TXT = "txt"
    M3U8 = "m3u8"
    XSPF = "xspf"
    JSON = "json"


class ExportWarning(BaseModel):
    code: str
    message: str
    playlist_id: str | None = None


class OpenPlaylistBundle(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_uri: Literal[OPEN_PLAYLIST_BUNDLE_SCHEMA] = Field(
        default=OPEN_PLAYLIST_BUNDLE_SCHEMA,
        alias="$schema",
    )
    schema_version: Literal[1] = 1
    source_provider: str
    playlists: list[Playlist] = Field(default_factory=list)
    warnings: list[ExportWarning] = Field(default_factory=list)


class ExportManifestEntry(BaseModel):
    playlist_id: str
    playlist_name: str
    filename: str
    status: Literal["ok", "warning", "error"] = "ok"
    track_count: int = 0
    warning_codes: list[str] = Field(default_factory=list)


class ExportManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_uri: Literal[EXPORT_MANIFEST_SCHEMA] = Field(
        default=EXPORT_MANIFEST_SCHEMA,
        alias="$schema",
    )
    schema_version: Literal[1] = 1
    format: ExportFormat
    source_provider: str
    playlist_count: int
    entries: list[ExportManifestEntry] = Field(default_factory=list)
    warnings: list[ExportWarning] = Field(default_factory=list)

