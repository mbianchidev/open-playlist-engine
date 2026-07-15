"""Immutable, bounded public playlist snapshots and portable serializers."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import secrets
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from app.core.models import MediaType, Playlist, PlaylistKind, Track

SNAPSHOT_SCHEMA_VERSION = "1.0"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_SAFE_FILENAME = re.compile(r"[^a-z0-9]+")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t")


class ShareVisibility(StrEnum):
    PUBLIC = "public"
    UNLISTED = "unlisted"


class PortableFormat(StrEnum):
    JSON = "json"
    CSV = "csv"
    TXT = "txt"
    M3U8 = "m3u8"
    XSPF = "xspf"


class SnapshotLimitError(ValueError):
    pass


class SharedSource(BaseModel):
    provider: str = Field(max_length=64)
    url: str | None = Field(default=None, max_length=2048)


class SharedTrack(BaseModel):
    position: int
    title: str = Field(max_length=500)
    artist: str = Field(max_length=500)
    album: str | None = Field(default=None, max_length=500)
    duration_s: int | None = Field(default=None, ge=0)
    release_year: int | None = None
    explicit: bool | None = None
    isrc: str | None = Field(default=None, max_length=32)
    artwork_url: str | None = Field(default=None, max_length=2048)
    source_url: str | None = Field(default=None, max_length=2048)
    media_type: MediaType = MediaType.TRACK
    unsupported_reason: str | None = Field(default=None, max_length=500)


class SharedPlaylistSnapshot(BaseModel):
    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    name: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    cover_url: str | None = Field(default=None, max_length=2048)
    attribution: str | None = Field(default=None, max_length=500)
    source: SharedSource
    tracks: list[SharedTrack] = Field(default_factory=list)


@dataclass(frozen=True)
class ExportedPlaylist:
    filename: str
    media_type: str
    content: bytes


def generate_share_token() -> str:
    return secrets.token_urlsafe(32)


def hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def build_shared_snapshot(
    playlist: Playlist,
    *,
    provider: str,
    playlist_id: str,
    attribution: str | None,
    approved_artwork_hosts: set[str],
    max_tracks: int,
    max_bytes: int,
) -> SharedPlaylistSnapshot:
    if len(playlist.tracks) > max_tracks:
        raise SnapshotLimitError(
            f"playlist exceeds public share track limit of {max_tracks}"
        )

    tracks = [
        SharedTrack(
            position=track.position if track.position is not None else index,
            title=_single_line(track.title, 500) or "Untitled track",
            artist=_single_line(track.artist, 500) or "Unknown artist",
            album=_single_line(track.album, 500),
            duration_s=max(0, track.duration_s) if track.duration_s is not None else None,
            release_year=track.release_year,
            explicit=track.explicit,
            isrc=_single_line(track.isrc, 32),
            artwork_url=_approved_https_url(track.artwork_uri, approved_artwork_hosts),
            source_url=_track_source_url(provider, track),
            media_type=track.media_type,
            unsupported_reason=_single_line(track.unsupported_reason, 500),
        )
        for index, track in enumerate(playlist.tracks)
    ]
    snapshot = SharedPlaylistSnapshot(
        name=_single_line(playlist.name, 500) or "Untitled playlist",
        description=_multiline(playlist.description, 4000),
        cover_url=_approved_https_url(playlist.photo, approved_artwork_hosts),
        attribution=_single_line(attribution, 500),
        source=SharedSource(
            provider=_single_line(provider, 64) or "unknown",
            url=_playlist_source_url(provider, playlist_id, playlist.kind),
        ),
        tracks=tracks,
    )
    _ensure_bytes(snapshot.model_dump_json().encode(), max_bytes, "snapshot")
    return snapshot


def snapshot_to_playlist(snapshot: SharedPlaylistSnapshot) -> Playlist:
    return Playlist(
        name=snapshot.name,
        description=snapshot.description,
        photo=snapshot.cover_url,
        kind=PlaylistKind.STANDARD,
        tracks=[
            Track(
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration_s=track.duration_s,
                release_year=track.release_year,
                explicit=track.explicit,
                isrc=track.isrc,
                artwork_uri=track.artwork_url,
                position=track.position,
                media_type=track.media_type,
                unsupported_reason=track.unsupported_reason,
            )
            for track in snapshot.tracks
        ],
    )


def render_share_html(
    snapshot: SharedPlaylistSnapshot,
    *,
    canonical_url: str,
    app_url: str,
    visibility: ShareVisibility,
) -> str:
    title = html.escape(snapshot.name, quote=True)
    summary = snapshot.description or f"{len(snapshot.tracks)} tracks"
    if snapshot.attribution:
        summary = f"{summary} — {snapshot.attribution}"
    description = html.escape(_single_line(summary, 500) or "", quote=True)
    canonical = html.escape(canonical_url, quote=True)
    redirect = html.escape(app_url, quote=True)
    robots = "index,follow" if visibility is ShareVisibility.PUBLIC else "noindex,nofollow"
    image = (
        f'<meta property="og:image" content="{html.escape(snapshot.cover_url, quote=True)}">'
        if snapshot.cover_url
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="referrer" content="no-referrer">
    <meta name="robots" content="{robots}">
    <meta name="description" content="{description}">
    <meta property="og:type" content="music.playlist">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{description}">
    <meta property="og:url" content="{canonical}">
    {image}
    <link rel="canonical" href="{canonical}">
    <meta http-equiv="refresh" content="0;url={redirect}">
    <title>{title} — Open Playlist Engine</title>
  </head>
  <body>
    <p><a href="{redirect}">Open shared playlist</a></p>
  </body>
</html>
"""


def serialize_snapshot(
    snapshot: SharedPlaylistSnapshot,
    format_: PortableFormat,
    *,
    max_bytes: int,
) -> ExportedPlaylist:
    slug = _filename_slug(snapshot.name)
    if format_ is PortableFormat.JSON:
        content = (
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        ).encode()
        exported = ExportedPlaylist(f"{slug}.json", "application/json", content)
    elif format_ is PortableFormat.CSV:
        exported = ExportedPlaylist(
            f"{slug}.csv",
            "text/csv",
            _csv_content(snapshot),
        )
    elif format_ is PortableFormat.TXT:
        exported = ExportedPlaylist(
            f"{slug}.txt",
            "text/plain",
            _txt_content(snapshot),
        )
    elif format_ is PortableFormat.M3U8:
        exported = ExportedPlaylist(
            f"{slug}.m3u8",
            "audio/x-mpegurl",
            _m3u8_content(snapshot),
        )
    else:
        exported = ExportedPlaylist(
            f"{slug}.xspf",
            "application/xspf+xml",
            _xspf_content(snapshot),
        )
    _ensure_bytes(exported.content, max_bytes, "export")
    return exported


def _csv_content(snapshot: SharedPlaylistSnapshot) -> bytes:
    output = io.StringIO(newline="")
    fields = [
        "position",
        "title",
        "artist",
        "album",
        "duration_seconds",
        "release_year",
        "explicit",
        "isrc",
        "source_url",
        "artwork_url",
        "media_type",
        "unsupported_reason",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for track in snapshot.tracks:
        writer.writerow(
            {
                "position": track.position,
                "title": _spreadsheet_safe(track.title),
                "artist": _spreadsheet_safe(track.artist),
                "album": _spreadsheet_safe(track.album),
                "duration_seconds": track.duration_s,
                "release_year": track.release_year,
                "explicit": track.explicit,
                "isrc": _spreadsheet_safe(track.isrc),
                "source_url": _spreadsheet_safe(track.source_url),
                "artwork_url": _spreadsheet_safe(track.artwork_url),
                "media_type": track.media_type.value,
                "unsupported_reason": _spreadsheet_safe(track.unsupported_reason),
            }
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode()


def _txt_content(snapshot: SharedPlaylistSnapshot) -> bytes:
    lines = [snapshot.name]
    if snapshot.attribution:
        lines.append(snapshot.attribution)
    lines.append("")
    lines.extend(
        f"{index}. {track.artist} - {track.title}"
        for index, track in enumerate(snapshot.tracks, start=1)
    )
    return ("\n".join(lines) + "\n").encode()


def _m3u8_content(snapshot: SharedPlaylistSnapshot) -> bytes:
    lines = ["#EXTM3U", f"#PLAYLIST:{snapshot.name}"]
    for track in snapshot.tracks:
        duration = track.duration_s if track.duration_s is not None else -1
        lines.append(f"#EXTINF:{duration},{track.artist} - {track.title}")
        if track.source_url:
            lines.append(track.source_url)
        else:
            lines.append(f"# OPE-MISSING-URI {track.position}")
    return ("\n".join(lines) + "\n").encode()


def _xspf_content(snapshot: SharedPlaylistSnapshot) -> bytes:
    namespace = "http://xspf.org/ns/0/"
    ET.register_namespace("", namespace)
    playlist = ET.Element(f"{{{namespace}}}playlist", {"version": "1"})
    ET.SubElement(playlist, f"{{{namespace}}}title").text = snapshot.name
    track_list = ET.SubElement(playlist, f"{{{namespace}}}trackList")
    for track in snapshot.tracks:
        node = ET.SubElement(track_list, f"{{{namespace}}}track")
        ET.SubElement(node, f"{{{namespace}}}title").text = track.title
        ET.SubElement(node, f"{{{namespace}}}creator").text = track.artist
        if track.album:
            ET.SubElement(node, f"{{{namespace}}}album").text = track.album
        if track.duration_s is not None:
            ET.SubElement(node, f"{{{namespace}}}duration").text = str(
                track.duration_s * 1000
            )
        if track.source_url:
            ET.SubElement(node, f"{{{namespace}}}location").text = track.source_url
        if track.artwork_url:
            ET.SubElement(node, f"{{{namespace}}}image").text = track.artwork_url
    return ET.tostring(playlist, encoding="utf-8", xml_declaration=True)


def _single_line(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value.replace("\r", " ").replace("\n", " "))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned[:limit] or None


def _multiline(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    return cleaned.strip()[:limit] or None


def _approved_https_url(value: str | None, approved_hosts: set[str]) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    approved = {item.lower().strip(".") for item in approved_hosts if item}
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not any(host == item or host.endswith(f".{item}") for item in approved)
    ):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def _playlist_source_url(
    provider: str, playlist_id: str, kind: PlaylistKind
) -> str | None:
    if kind is PlaylistKind.LIKED_TRACKS or not playlist_id:
        return None
    encoded = quote(playlist_id, safe="")
    if provider == "spotify":
        return f"https://open.spotify.com/playlist/{encoded}"
    if provider == "ytmusic":
        return f"https://music.youtube.com/playlist?list={encoded}"
    if provider == "tidal":
        return f"https://tidal.com/browse/playlist/{encoded}"
    return None


def _track_source_url(provider: str, track: Track) -> str | None:
    raw = track.provider_uris.get(provider)
    if not raw:
        return None
    if raw.startswith("https://"):
        parsed = urlsplit(raw)
        allowed = {
            "spotify": {"open.spotify.com"},
            "ytmusic": {"music.youtube.com", "www.youtube.com"},
            "tidal": {"tidal.com", "listen.tidal.com"},
            "applemusic": {"music.apple.com"},
        }.get(provider, set())
        if parsed.hostname in allowed and not parsed.username and not parsed.password:
            return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
        return None
    prefix, separator, identifier = raw.rpartition(":")
    if not separator or not identifier:
        identifier = raw
        prefix = ""
    encoded = quote(identifier, safe="")
    if provider == "spotify" and (prefix.endswith("track") or ":" not in raw):
        return f"https://open.spotify.com/track/{encoded}"
    if provider == "ytmusic":
        return f"https://music.youtube.com/watch?v={encoded}"
    if provider == "tidal" and (prefix.endswith("track") or ":" not in raw):
        return f"https://tidal.com/browse/track/{encoded}"
    return None


def _spreadsheet_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    single_line = _single_line(value, 4096) or ""
    if single_line.startswith(_FORMULA_PREFIXES):
        return f"'{single_line}"
    return single_line


def _filename_slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    )
    slug = _SAFE_FILENAME.sub("-", ascii_value).strip("-")[:80]
    return slug or "playlist"


def _ensure_bytes(content: bytes, max_bytes: int, kind: str) -> None:
    if len(content) > max_bytes:
        raise SnapshotLimitError(f"{kind} exceeds public share byte limit of {max_bytes}")

