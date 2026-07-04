"""Helpers for comparing source, target and historical migrated tracks."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from app.core.models import Track

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat|ft|with)\.?\s.*$", re.IGNORECASE)
_NOISE = re.compile(r"[^a-z0-9]+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = _FEAT.sub("", value.lower())
    return _NOISE.sub(" ", value).strip()


def track_keys(track: Track) -> set[str]:
    return keys_from_values(
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_s=track.duration_s,
        isrc=track.isrc,
        track_id=track.id,
        source_item_id=track.source_item_id,
        provider_uris=track.provider_uris,
    )


def keys_from_values(
    *,
    title: str | None,
    artist: str | None,
    album: str | None = None,
    duration_s: int | None = None,
    isrc: str | None = None,
    track_id: str | None = None,
    source_item_id: str | None = None,
    provider_uris: dict[str, str] | None = None,
) -> set[str]:
    keys: set[str] = set()
    if isrc:
        keys.add(f"isrc:{isrc.upper()}")
    for value in (track_id, source_item_id):
        if value:
            keys.add(f"id:{value.lower()}")
    for uri in (provider_uris or {}).values():
        keys.update(uri_keys(uri))

    title_norm = normalize_text(title)
    artist_norm = normalize_text(artist)
    if title_norm and artist_norm:
        album_norm = normalize_text(album)
        duration_bucket = str(round(duration_s / 5) * 5) if duration_s else ""
        keys.add(f"sig:{title_norm}|{artist_norm}|{album_norm}|{duration_bucket}")
        keys.add(f"song:{title_norm}|{artist_norm}")
    return keys


def keys_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    title: str | None,
    artist: str | None,
    album: str | None = None,
    duration_s: int | None = None,
    isrc: str | None = None,
) -> set[str]:
    metadata = metadata or {}
    provider_uris = metadata.get("provider_uris")
    return keys_from_values(
        title=str(metadata.get("title") or title or ""),
        artist=str(metadata.get("artist") or artist or ""),
        album=str(metadata.get("album") or album or "") or None,
        duration_s=_int_or_none(metadata.get("duration_s")) or duration_s,
        isrc=str(metadata.get("isrc") or isrc or "") or None,
        track_id=str(metadata.get("id") or "") or None,
        source_item_id=str(metadata.get("source_item_id") or "") or None,
        provider_uris=provider_uris if isinstance(provider_uris, dict) else None,
    )


def uri_keys(uri: str | None) -> set[str]:
    if not uri:
        return set()
    uri = uri.strip()
    if not uri:
        return set()
    lower = uri.lower()
    keys = {f"uri:{lower}"}
    provider_id = _provider_item_id(uri)
    if provider_id:
        keys.add(f"id:{provider_id.lower()}")
    return keys


def has_track_overlap(left: list[Track], right: list[Track]) -> bool:
    right_keys = set().union(*(track_keys(track) for track in right)) if right else set()
    return any(track_keys(track) & right_keys for track in left)


def track_selected(track: Track, wanted: set[str]) -> bool:
    if not wanted:
        return True
    position = str(track.position) if track.position is not None else None
    identifiers = {value for value in [track.id, track.source_item_id, position] if value}
    return bool(identifiers & wanted)


def filter_unmigrated_tracks(tracks: list[Track], migrated_keys: set[str]) -> list[Track]:
    if not migrated_keys:
        return tracks
    return [track for track in tracks if not track_keys(track) & migrated_keys]


def _provider_item_id(uri: str) -> str | None:
    parsed = urllib.parse.urlparse(uri)
    if parsed.query:
        video_id = urllib.parse.parse_qs(parsed.query).get("v")
        if video_id:
            return video_id[0]
    if "/track/" in uri:
        tail = uri.split("/track/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0] or None
    if ":" in uri and "//" not in uri:
        return uri.rsplit(":", 1)[-1] or None
    return uri or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
