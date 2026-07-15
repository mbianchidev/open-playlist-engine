from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass

from app.core.models import Playlist, Track
from app.imports.models import ImportIssue, ParsedTextImport

_DASH_SEPARATOR = re.compile(r"\s+(?:-|–|—)\s+")
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_HEADER_ALIASES = {
    "artist": "artist",
    "artists": "artist",
    "performer": "artist",
    "title": "title",
    "track": "title",
    "track title": "title",
    "song": "title",
    "album": "album",
    "record": "album",
    "isrc": "isrc",
    "duration": "duration_s",
    "duration s": "duration_s",
    "duration seconds": "duration_s",
    "seconds": "duration_s",
}


class ImportLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class TextImportLimits:
    max_bytes: int
    max_items: int
    max_line_chars: int
    max_field_chars: int


@dataclass(frozen=True)
class _Header:
    delimiter: str
    columns: dict[str, int]


def parse_track_text(
    text: str,
    *,
    name: str | None,
    limits: TextImportLimits,
) -> ParsedTextImport:
    size = len(text.encode("utf-8"))
    if size > limits.max_bytes:
        raise ImportLimitExceeded(
            f"pasted text exceeds the {limits.max_bytes} bytes input limit"
        )

    rows = [
        (line_number, line)
        for line_number, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    header = _detect_header(rows[0][1]) if rows else None
    data_rows = rows[1:] if header else rows
    if len(data_rows) > limits.max_items:
        raise ImportLimitExceeded(
            f"pasted text exceeds the {limits.max_items} items input limit"
        )

    issues: list[ImportIssue] = []
    tracks: list[Track] = []
    for line_number, raw in data_rows:
        if len(raw) > limits.max_line_chars:
            issues.append(
                ImportIssue(
                    line=line_number,
                    code="line_too_long",
                    message=(
                        f"Line exceeds the {limits.max_line_chars} character row limit."
                    ),
                    severity="error",
                    raw=_issue_raw(raw, limits.max_line_chars),
                )
            )
            continue
        fields = _header_fields(raw, header) if header else _positional_fields(raw)
        track = _track_from_fields(
            fields,
            line_number=line_number,
            raw=raw,
            position=len(tracks),
            limits=limits,
            issues=issues,
        )
        if track is not None:
            tracks.append(track)

    normalized_name = (name or "").strip() or "Imported track list"
    if len(normalized_name) > limits.max_field_chars:
        raise ImportLimitExceeded(
            f"playlist name exceeds the {limits.max_field_chars} character field limit"
        )
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    fingerprint = hashlib.sha256(
        f"{normalized_name}\0{normalized_text}".encode()
    ).hexdigest()
    return ParsedTextImport(
        playlist=Playlist(
            id=f"text:{fingerprint[:32]}",
            name=normalized_name,
            tracks=tracks,
        ),
        issues=issues,
        fingerprint=fingerprint,
    )


def _detect_header(raw: str) -> _Header | None:
    for delimiter in ("\t", ",", ";", "|"):
        if delimiter not in raw:
            continue
        values = _read_delimited(raw, delimiter)
        columns: dict[str, int] = {}
        for index, value in enumerate(values):
            key = _HEADER_ALIASES.get(_normalize_header(value))
            if key and key not in columns:
                columns[key] = index
        if "title" in columns and len(columns) >= 2:
            return _Header(delimiter=delimiter, columns=columns)
    return None


def _normalize_header(value: str) -> str:
    return re.sub(r"[_-]+", " ", value.strip().lower())


def _read_delimited(raw: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([raw], delimiter=delimiter, skipinitialspace=True))
    except csv.Error:
        return raw.split(delimiter)


def _header_fields(raw: str, header: _Header) -> dict[str, str]:
    values = _read_delimited(raw, header.delimiter)
    return {
        key: values[index].strip() if index < len(values) else ""
        for key, index in header.columns.items()
    }


def _positional_fields(raw: str) -> dict[str, str]:
    if "\t" in raw:
        values = [value.strip() for value in _read_delimited(raw, "\t")]
        keys = ("artist", "title", "album", "isrc")
        return {key: values[index] for index, key in enumerate(keys) if index < len(values)}
    parts = _DASH_SEPARATOR.split(raw.strip(), maxsplit=1)
    if len(parts) == 2:
        return {"artist": parts[0].strip(), "title": parts[1].strip()}
    return {"artist": "", "title": raw.strip()}


def _track_from_fields(
    fields: dict[str, str],
    *,
    line_number: int,
    raw: str,
    position: int,
    limits: TextImportLimits,
    issues: list[ImportIssue],
) -> Track | None:
    title = fields.get("title", "").strip()
    artist = fields.get("artist", "").strip()
    album = fields.get("album", "").strip() or None
    isrc = fields.get("isrc", "").strip().upper() or None

    if not title:
        issues.append(
            ImportIssue(
                line=line_number,
                code="missing_title",
                message="A track title is required.",
                severity="error",
                raw=raw,
            )
        )
        return None
    for field_name, value in (("title", title), ("artist", artist), ("album", album or "")):
        if len(value) > limits.max_field_chars:
            issues.append(
                ImportIssue(
                    line=line_number,
                    code="field_too_long",
                    message=(
                        f"{field_name.title()} exceeds the "
                        f"{limits.max_field_chars} character field limit."
                    ),
                    severity="error",
                    raw=raw,
                )
            )
            return None
    if not artist:
        issues.append(
            ImportIssue(
                line=line_number,
                code="missing_artist",
                message="Artist is missing; matching will rely on the title.",
                raw=raw,
            )
        )
    if isrc and not _ISRC.fullmatch(isrc):
        issues.append(
            ImportIssue(
                line=line_number,
                code="invalid_isrc",
                message="ISRC must contain 12 letters or digits in standard ISRC form.",
                raw=raw,
            )
        )
        isrc = None

    duration_s = _duration(fields.get("duration_s", ""), line_number, raw, issues)
    return Track(
        title=title,
        artist=artist,
        album=album,
        duration_s=duration_s,
        isrc=isrc,
        position=position,
        source_item_id=f"text:{line_number}",
        metadata={"source_line": line_number},
    )


def _duration(
    raw_duration: str,
    line_number: int,
    raw: str,
    issues: list[ImportIssue],
) -> int | None:
    value = raw_duration.strip()
    if not value:
        return None
    try:
        duration = round(float(value))
    except ValueError:
        duration = -1
    if duration < 0:
        issues.append(
            ImportIssue(
                line=line_number,
                code="invalid_duration",
                message="Duration must be a non-negative number of seconds.",
                raw=raw,
            )
        )
        return None
    return duration


def _issue_raw(raw: str, max_chars: int) -> str:
    if len(raw) <= max_chars:
        return raw
    return raw[: max(0, max_chars - 1)] + "…"
