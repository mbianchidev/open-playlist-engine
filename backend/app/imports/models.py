from __future__ import annotations

from datetime import datetime
from enum import StrEnum

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
