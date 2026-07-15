from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.core.models import Playlist


class ImportIssue(BaseModel):
    line: int | None = None
    code: str
    message: str
    severity: Literal["warning", "error"] = "warning"
    raw: str | None = None


class ParsedTextImport(BaseModel):
    playlist: Playlist
    issues: list[ImportIssue] = Field(default_factory=list)
    fingerprint: str


class ResolvedPlaylistUrl(BaseModel):
    provider: str
    resource_id: str
    canonical_url: str
    source_label: str
    metadata: dict[str, str] = Field(default_factory=dict)

