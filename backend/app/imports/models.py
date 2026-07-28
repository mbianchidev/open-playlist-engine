from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import Playlist


class ImportFormat(StrEnum):
    TXT = "txt"
    CSV = "csv"
    M3U = "m3u"
    M3U8 = "m3u8"
    PLS = "pls"
    WPL = "wpl"
    XSPF = "xspf"
    XML = "xml"
    JSON = "json"


class ImportIssueSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class ImportLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_playlists: int = Field(default=100, gt=0)
    max_tracks: int = Field(default=5_000, gt=0)
    max_issues: int = Field(default=200, gt=0)
    spool_memory_bytes: int = Field(default=1024 * 1024, gt=0)


class ImportIssue(BaseModel):
    severity: ImportIssueSeverity
    code: str
    message: str
    line_or_item: int | str | None = None
    playlist_name: str | None = None
    raw_excerpt: str | None = None


class ImportParseResult(BaseModel):
    detected_format: ImportFormat
    encoding: str | None = None
    file_size: int
    playlists: list[Playlist]
    issues: list[ImportIssue] = Field(default_factory=list)
    playlist_count: int
    track_count: int
    duplicate_count: int = 0
    malformed_count: int = 0
    unsupported_count: int = 0


class LocalImportPreview(ImportParseResult):
    id: str
    filename: str
    status: str
    expires_at: datetime
    limits: ImportLimits


# --------------------------------------------------------------------------- #
# Public URL and pasted-text imports.
#
# These share the same ``LocalPlaylistImport`` lease-backed table and lifecycle
# as local-file imports (see app.imports.service), but use a distinct issue
# model since they are validated line-by-line rather than parsed from a file
# format, and a distinct preview view since there is no uploaded filename.
# --------------------------------------------------------------------------- #
class SourceImportIssue(BaseModel):
    line: int | None = None
    code: str
    message: str
    severity: Literal["warning", "error"] = "warning"
    raw: str | None = None


class ParsedTextImport(BaseModel):
    playlist: Playlist
    issues: list[SourceImportIssue] = Field(default_factory=list)
    fingerprint: str


class ResolvedPlaylistUrl(BaseModel):
    provider: str
    resource_id: str
    canonical_url: str
    source_label: str
    metadata: dict[str, str] = Field(default_factory=dict)


class SourceImportPreview(BaseModel):
    id: str
    source_kind: str
    source_provider: str
    source_label: str
    source_locator: str
    status: str
    expires_at: datetime
    playlist: Playlist
    issues: list[SourceImportIssue] = Field(default_factory=list)
    track_count: int
    unsupported_count: int
