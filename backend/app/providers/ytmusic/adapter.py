"""YouTube Music adapter — default write path (via ``ytmusicapi``).

Unofficial: no quota but it can break and carries account-flag risk, so it is
marked EXPERIMENTAL and only registered when ``OPE_YTMUSIC_ENABLED`` is set.
It has no ISRC, so matching is text-only and leans on the evidence graph and the
review step. Header-paste auth is offered only in self-host mode.

This PR implements the WRITE primitives (``create_playlist`` / ``add_tracks``).
``ytmusicapi`` is synchronous, so calls run in a worker thread. The client is
built through an injectable factory so the conformance suite can drive the
adapter against an in-memory fake instead of the live, unofficial API.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol

from app.core.adapter import (
    AddItemResult,
    AuthChallenge,
    AuthExpired,
    AuthKind,
    AuthStrategy,
    ChallengeShape,
    CreatePlaylistSpec,
    NotFound,
    ProviderCredential,
    ProviderError,
    ProviderInfo,
    TrackCandidate,
    Unsupported,
)
from app.core.capabilities import (
    Capability,
    CapabilityDescriptor,
    OrderingGuarantee,
    SearchMode,
    Stability,
)
from app.core.models import Playlist, PlaylistRef, Track
from app.core.registry import register
from app.settings import get_settings


class YTMusicClient(Protocol):
    """The narrow slice of ``ytmusicapi.YTMusic`` this adapter depends on."""

    def get_library_playlists(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def get_playlist(self, playlistId: str, limit: int | None = 100) -> dict[str, Any]: ...

    def create_playlist(
        self,
        title: str,
        description: str,
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
        source_playlist: str | None = None,
    ) -> str | dict[str, Any]: ...

    def add_playlist_items(
        self,
        playlistId: str,
        videoIds: list[str] | None = None,
        source_playlist: str | None = None,
        duplicates: bool = False,
    ) -> str | dict[str, Any]: ...

    def search(
        self, query: str, filter: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...


ClientFactory = Callable[[ProviderCredential], YTMusicClient]


def _default_client_factory(cred: ProviderCredential) -> YTMusicClient:
    """Build a real ``YTMusic`` client from stored credentials.

    Not exercised in CI (the conformance suite injects a fake). ``cred.extra``
    carries the ``ytmusicapi`` auth payload (oauth token JSON or pasted headers).
    """
    from ytmusicapi import YTMusic

    auth = cred.extra.get("auth") or cred.access_token
    if not auth:
        raise AuthExpired("missing ytmusic credentials")
    return YTMusic(auth)


def _video_id(uri: str) -> str:
    """Extract a YouTube videoId from a watch URL, ``*:video:<id>`` URI, or bare id."""
    uri = uri.strip()
    if "watch?v=" in uri:
        query = urllib.parse.urlparse(uri).query
        found = urllib.parse.parse_qs(query).get("v")
        if found:
            return found[0]
    if ":" in uri and "//" not in uri:
        return uri.rsplit(":", 1)[-1]
    return uri


def _duration_s(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _artist_names(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    names = [artist.get("name", "") for artist in value if isinstance(artist, dict)]
    return ", ".join(name for name in names if name)


def _playlist_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or "")


def _playlist_id(item: dict[str, Any]) -> str | None:
    value = item.get("playlistId") or item.get("id") or item.get("playlist_id")
    return str(value) if value else None


def _track_from_video(item: dict[str, Any], position: int) -> Track | None:
    video_id = item.get("videoId") or item.get("video_id")
    title = item.get("title") or item.get("name")
    if not video_id or not title:
        return None
    duration = item.get("duration_seconds") or _duration_s(item.get("duration"))
    uri = f"ytmusic:video:{video_id}"
    return Track(
        id=str(video_id),
        title=str(title),
        artist=_artist_names(item.get("artists")),
        album=(item.get("album") or {}).get("name")
        if isinstance(item.get("album"), dict)
        else None,
        duration_s=duration,
        explicit=item.get("isExplicit"),
        provider_uris={"ytmusic": uri},
        position=position,
        source_item_id=str(video_id),
    )


def _auth_from_headers(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        raise ProviderError("YouTube Music headers are required")
    if raw.startswith("{"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ProviderError("YouTube Music auth JSON must be an object")
        return parsed

    from ytmusicapi import setup

    parsed = setup(filepath=None, headers_raw=raw)
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise ProviderError("ytmusicapi returned invalid auth JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("ytmusicapi did not return a valid auth payload")
    return parsed


def _succeeded(response: str | dict[str, Any]) -> bool:
    return isinstance(response, dict) and "SUCCEEDED" in str(response.get("status", ""))


class YTMusicAuth(AuthStrategy):
    kind = AuthKind.OAUTH_DEVICE

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        if get_settings().allow_header_paste:
            return AuthChallenge(
                shape=ChallengeShape.FORM,
                instructions=(
                    "Self-host only: paste request headers from an authenticated "
                    "music.youtube.com session, or use device-code auth."
                ),
                form_schema={"headers_raw": {"type": "string", "format": "textarea"}},
            )
        # Device-code flow (Google 'TV & Limited Input' OAuth client, post-2024).
        return AuthChallenge(
            shape=ChallengeShape.DEVICE_CODE,
            user_code="TODO",
            verification_url="https://www.google.com/device",
            poll_interval_s=5,
        )

    async def complete(self, *, user_id: str, callback: dict) -> ProviderCredential:
        if not get_settings().allow_header_paste:
            raise Unsupported("YouTube Music header-paste auth is disabled outside self-host mode")
        headers_raw = callback.get("headers_raw")
        if not isinstance(headers_raw, str):
            raise ProviderError("YouTube Music header auth requires headers_raw")
        auth_payload = await asyncio.to_thread(_auth_from_headers, headers_raw)
        if not any(str(key).lower() == "authorization" for key in auth_payload):
            raise ProviderError("YouTube Music headers must include Authorization")
        if not any(str(key).lower() == "cookie" for key in auth_payload):
            raise ProviderError("YouTube Music headers must include Cookie")
        return ProviderCredential(
            account_id="ytmusic-local",
            provider="ytmusic",
            auth_kind=AuthKind.HEADER_PASTE,
            extra={"auth": auth_payload, "display_name": "YouTube Music"},
        )

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        return cred

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


class YTMusicAdapter:
    info = ProviderInfo(
        name="ytmusic",
        display_name="YouTube Music",
        auth_kind=AuthKind.OAUTH_DEVICE,
        capabilities=CapabilityDescriptor(
            capabilities={
                Capability.READ_PLAYLISTS,
                Capability.READ_TRACKS,
                Capability.CREATE_PLAYLIST,
                Capability.ADD_TRACKS,
                Capability.SET_DESCRIPTION,
            },
            has_isrc=False,
            search_modes=[SearchMode.TEXT],
            official=False,
            stability=Stability.EXPERIMENTAL,
            max_add_batch=100,
            ordering=OrderingGuarantee.BEST_EFFORT,
            warning="Unofficial API — no quota but may break and carries account-flag risk.",
        ),
    )
    auth = YTMusicAuth()

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        # Injecting a factory lets the conformance suite supply an in-memory client.
        self._client_factory = client_factory or _default_client_factory

    def _client(self, cred: ProviderCredential) -> YTMusicClient:
        return self._client_factory(cred)

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        client = self._client(cred)
        rows = await asyncio.to_thread(lambda: client.get_library_playlists(limit=1000))
        for item in rows:
            playlist_id = _playlist_id(item)
            if not playlist_id:
                continue
            count = item.get("count") or item.get("trackCount")
            yield PlaylistRef(
                id=playlist_id,
                name=_playlist_title(item),
                track_count=count if isinstance(count, int) else None,
            )

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        client = self._client(cred)
        playlist = await asyncio.to_thread(lambda: client.get_playlist(ref.id, limit=5000))
        if playlist.get("error"):
            raise NotFound(ref.id)
        tracks = playlist.get("tracks")
        if not isinstance(tracks, list):
            raise ProviderError("ytmusic playlist response did not include tracks")
        for position, item in enumerate(tracks):
            if not isinstance(item, dict):
                continue
            track = _track_from_video(item, position)
            if track is not None:
                yield track

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        client = self._client(cred)
        raw = await asyncio.to_thread(lambda: client.get_playlist(ref.id, limit=5000))
        if raw.get("error"):
            raise NotFound(ref.id)
        raw_tracks = raw.get("tracks")
        if not isinstance(raw_tracks, list):
            raise ProviderError("ytmusic playlist response did not include tracks")
        tracks = [
            track
            for position, item in enumerate(raw_tracks)
            if isinstance(item, dict)
            for track in [_track_from_video(item, position)]
            if track is not None
        ]
        name = str(raw.get("title") or raw.get("name") or ref.name)
        return Playlist(
            id=ref.id,
            name=name,
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            tracks=tracks,
        )

    async def test_connection(self, cred: ProviderCredential) -> None:
        client = self._client(cred)
        await asyncio.to_thread(lambda: client.search("test", filter="songs", limit=1))

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        query = f"{track.title} {track.artist}".strip()
        if track.album:
            query = f"{query} {track.album}".strip()
        client = self._client(cred)
        results = await asyncio.to_thread(lambda: client.search(query, filter="songs", limit=limit))
        candidates: list[TrackCandidate] = []
        for item in results:
            video_id = item.get("videoId")
            if not video_id:
                continue
            candidates.append(
                TrackCandidate(
                    provider_track_id=video_id,
                    uri=f"ytmusic:video:{video_id}",
                    title=item.get("title") or "",
                    artist=_artist_names(item.get("artists")),
                    album=(item.get("album") or {}).get("name")
                    if isinstance(item.get("album"), dict)
                    else None,
                    duration_s=item.get("duration_seconds") or _duration_s(item.get("duration")),
                    explicit=item.get("isExplicit"),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        video_id = _video_id(uri)
        return bool(video_id)

    # WRITE ----------------------------------------------------------------- #
    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        client = self._client(cred)
        privacy = "PUBLIC" if spec.public else "PRIVATE"
        result = await asyncio.to_thread(
            client.create_playlist, spec.name, spec.description or "", privacy
        )
        if not isinstance(result, str):
            raise ProviderError(f"ytmusic create_playlist failed: {result}")
        return result

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        client = self._client(cred)
        batch = max(1, self.info.capabilities.max_add_batch)
        uris = list(uris)
        results: list[AddItemResult] = []
        position = 0
        for start in range(0, len(uris), batch):
            chunk = uris[start : start + batch]
            video_ids = [_video_id(u) for u in chunk]
            # duplicates=True: a migration must preserve the source's repeats.
            response = await asyncio.to_thread(
                client.add_playlist_items, playlist_id, video_ids, None, True
            )
            ok = _succeeded(response)
            for uri in chunk:
                if ok:
                    results.append(AddItemResult(uri=uri, ok=True, position=position))
                    position += 1
                else:
                    results.append(
                        AddItemResult(uri=uri, ok=False, error=f"ytmusic add failed: {response}")
                    )
        return results


def _build() -> YTMusicAdapter | None:
    if not get_settings().ytmusic_enabled:
        return None
    return register(YTMusicAdapter())


adapter = _build()
