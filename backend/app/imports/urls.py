from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Collection

from app.imports.models import ResolvedPlaylistUrl

_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{10,64}$")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_APPLE_ID = re.compile(r"^(?:pl|p)\.[A-Za-z0-9._-]{2,160}$")
_TIDAL_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_OPEN_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")


class UnsafePlaylistUrl(ValueError):
    pass


def resolve_playlist_url(
    value: str,
    *,
    open_playlist_hosts: Collection[str],
    max_length: int = 2048,
) -> ResolvedPlaylistUrl:
    parsed, host = validate_https_url(
        value,
        allowed_hosts={
            "open.spotify.com",
            "music.youtube.com",
            "www.youtube.com",
            "youtube.com",
            "music.apple.com",
            "tidal.com",
            "listen.tidal.com",
            *(_normalize_host(item) for item in open_playlist_hosts),
        },
        max_length=max_length,
    )
    path_parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]

    if host == "open.spotify.com":
        return _resolve_spotify(path_parts)
    if host in {"music.youtube.com", "www.youtube.com", "youtube.com"}:
        return _resolve_youtube(parsed)
    if host == "music.apple.com":
        return _resolve_apple(path_parts)
    if host in {"tidal.com", "listen.tidal.com"}:
        return _resolve_tidal(path_parts)
    if host in {_normalize_host(item) for item in open_playlist_hosts}:
        return _resolve_open_playlist(parsed, host, path_parts)
    raise UnsafePlaylistUrl("unsupported playlist URL")


def validate_https_url(
    value: str,
    *,
    allowed_hosts: Collection[str],
    max_length: int = 2048,
) -> tuple[urllib.parse.SplitResult, str]:
    url = value.strip()
    if not url:
        raise UnsafePlaylistUrl("playlist URL is required")
    if len(url) > max_length:
        raise UnsafePlaylistUrl(f"playlist URL exceeds {max_length} characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafePlaylistUrl("playlist URL is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise UnsafePlaylistUrl("playlist URLs must use HTTPS")
    if parsed.username or parsed.password:
        raise UnsafePlaylistUrl("playlist URLs cannot contain credentials")
    if port not in {None, 443}:
        raise UnsafePlaylistUrl("playlist URLs must use the default HTTPS port")
    host = _normalize_host(parsed.hostname or "")
    if not host:
        raise UnsafePlaylistUrl("playlist URL host is required")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafePlaylistUrl("IP-address playlist URLs are not allowed")
    normalized_allowed = {_normalize_host(item) for item in allowed_hosts if item.strip()}
    if host not in normalized_allowed:
        raise UnsafePlaylistUrl(f"playlist URL host '{host}' is not supported")
    return parsed, host


def _normalize_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


def _resolve_spotify(parts: list[str]) -> ResolvedPlaylistUrl:
    if parts and parts[0].lower().startswith("intl-"):
        parts = parts[1:]
    if len(parts) != 2 or parts[0] != "playlist" or not _SPOTIFY_ID.fullmatch(parts[1]):
        raise UnsafePlaylistUrl("unsupported Spotify playlist URL")
    playlist_id = parts[1]
    return ResolvedPlaylistUrl(
        provider="spotify",
        resource_id=playlist_id,
        canonical_url=f"https://open.spotify.com/playlist/{playlist_id}",
        source_label="Spotify public playlist",
    )


def _resolve_youtube(parsed: urllib.parse.SplitResult) -> ResolvedPlaylistUrl:
    if parsed.path.rstrip("/") != "/playlist":
        raise UnsafePlaylistUrl("unsupported YouTube Music playlist URL")
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("list", [])
    playlist_id = values[0] if values else ""
    if not _YOUTUBE_ID.fullmatch(playlist_id):
        raise UnsafePlaylistUrl("YouTube Music playlist URL has an invalid list ID")
    return ResolvedPlaylistUrl(
        provider="ytmusic",
        resource_id=playlist_id,
        canonical_url=(
            "https://music.youtube.com/playlist?"
            + urllib.parse.urlencode({"list": playlist_id})
        ),
        source_label="YouTube Music public playlist",
    )


def _resolve_apple(parts: list[str]) -> ResolvedPlaylistUrl:
    if len(parts) < 3 or parts[1] != "playlist":
        raise UnsafePlaylistUrl("unsupported Apple Music playlist URL")
    storefront = parts[0].lower()
    playlist_id = parts[-1]
    if not re.fullmatch(r"[a-z]{2}", storefront) or not _APPLE_ID.fullmatch(playlist_id):
        raise UnsafePlaylistUrl("Apple Music playlist URL is malformed")
    return ResolvedPlaylistUrl(
        provider="applemusic",
        resource_id=playlist_id,
        canonical_url=f"https://music.apple.com/{storefront}/playlist/{playlist_id}",
        source_label="Apple Music public playlist",
        metadata={"storefront": storefront},
    )


def _resolve_tidal(parts: list[str]) -> ResolvedPlaylistUrl:
    if len(parts) == 3 and parts[:2] == ["browse", "playlist"]:
        playlist_id = parts[2]
    elif len(parts) == 2 and parts[0] == "playlist":
        playlist_id = parts[1]
    else:
        raise UnsafePlaylistUrl("unsupported TIDAL playlist URL")
    if not _TIDAL_ID.fullmatch(playlist_id):
        raise UnsafePlaylistUrl("TIDAL playlist URL has an invalid playlist ID")
    playlist_id = playlist_id.lower()
    return ResolvedPlaylistUrl(
        provider="tidal",
        resource_id=playlist_id,
        canonical_url=f"https://tidal.com/browse/playlist/{playlist_id}",
        source_label="TIDAL public playlist",
    )


def _resolve_open_playlist(
    parsed: urllib.parse.SplitResult,
    host: str,
    parts: list[str],
) -> ResolvedPlaylistUrl:
    if len(parts) != 2 or parts[0] not in {"share", "open-playlists"}:
        raise UnsafePlaylistUrl("unsupported Open Playlist Engine share URL")
    playlist_id = parts[1]
    if not _OPEN_PLAYLIST_ID.fullmatch(playlist_id):
        raise UnsafePlaylistUrl("Open Playlist Engine share URL has an invalid playlist ID")
    source_path = "share" if parts[0] == "share" else "open-playlists"
    return ResolvedPlaylistUrl(
        provider="openplaylist",
        resource_id=playlist_id,
        canonical_url=f"https://{host}/{source_path}/{playlist_id}",
        source_label=f"Open Playlist Engine ({host})",
        metadata={
            "fetch_url": f"https://{host}/open-playlists/{playlist_id}",
            "host": host,
        },
    )

