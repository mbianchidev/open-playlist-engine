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
import logging
import re
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

import httpx

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
    RateLimited,
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

logger = logging.getLogger(__name__)


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

_YTMUSIC_SCOPE = "https://www.googleapis.com/auth/youtube"
_OAUTH_GRANT_TYPE = "http://oauth.net/grant_type/device/1.0"
_OAUTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) "
    "Gecko/20100101 Firefox/88.0 Cobalt/Version"
)
_DEFAULT_POLL_INTERVAL_S = 5


@dataclass
class _PendingDeviceCode:
    user_id: str
    device_code: str
    expires_at: float
    interval_s: int


_PENDING_DEVICE_CODES: dict[str, _PendingDeviceCode] = {}


def _default_client_factory(cred: ProviderCredential) -> YTMusicClient:
    """Build a real ``YTMusic`` client from stored credentials.

    Not exercised in CI (the conformance suite injects a fake). ``cred.extra``
    carries the ``ytmusicapi`` auth payload (oauth token JSON or pasted headers).
    """
    from ytmusicapi import OAuthCredentials, YTMusic

    auth = cred.extra.get("auth") or cred.access_token
    if not auth:
        raise AuthExpired("missing ytmusic credentials")
    if cred.auth_kind is AuthKind.OAUTH_DEVICE:
        s = get_settings()
        if not _has_oauth_settings(s):
            raise ProviderError(
                "OPE_YTMUSIC_CLIENT_ID and OPE_YTMUSIC_CLIENT_SECRET are required "
                "for YouTube Music OAuth credentials"
            )
        return YTMusic(
            auth,
            oauth_credentials=OAuthCredentials(
                client_id=s.ytmusic_client_id,
                client_secret=s.ytmusic_client_secret,
            ),
        )
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


def _is_auth_error_message(message: str) -> bool:
    normalized = message.lower()
    return (
        "401" in normalized
        or "unauthorized" in normalized
        or "must be signed in" in normalized
        or re.search(
            r"""["'](?:logged_in|yt_li)["']\s*[:,][^}\]]*["']value["']\s*:\s*["']0["']""",
            normalized,
        )
        is not None
    )


def _auth_error(action: str, message: str) -> AuthExpired:
    return AuthExpired(
        f"YouTube Music {action} requires a signed-in session that can write playlists; "
        f"reconnect YouTube Music. {message}"
    )


async def _run_client_call(
    action: str, call: Callable[[], Any], *, playlist_id: str | None = None
) -> Any:
    try:
        return await asyncio.to_thread(call)
    except (KeyError, IndexError) as exc:
        message = str(exc)
        if _is_auth_error_message(message):
            logger.warning("ytmusic %s response indicates signed-out session error=%s", action, exc)
            raise _auth_error(action, message) from exc
        if "Unable to find" in str(exc):
            if playlist_id:
                logger.warning(
                    "ytmusic %s response missing expected content playlist_id=%s error=%s",
                    action,
                    playlist_id,
                    exc,
                )
                raise NotFound(
                    f"YouTube Music playlist '{playlist_id}' is unavailable or no longer accessible"
                ) from exc
            logger.warning("ytmusic %s response missing expected content error=%s", action, exc)
            raise ProviderError(f"YouTube Music {action} returned an unexpected response") from exc
        raise
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        if _is_auth_error_message(message):
            raise _auth_error(action, message) from exc
        if type(exc).__module__.startswith("ytmusicapi."):
            raise ProviderError(f"YouTube Music {action} failed: {message}") from exc
        raise


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
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("YouTube Music auth JSON is invalid") from exc
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


def _has_oauth_settings(settings) -> bool:
    return bool(settings.ytmusic_client_id and settings.ytmusic_client_secret)


def _has_partial_oauth_settings(settings) -> bool:
    return bool(settings.ytmusic_client_id) != bool(settings.ytmusic_client_secret)


def _oauth_settings_error() -> ProviderError:
    return ProviderError(
        "OPE_YTMUSIC_CLIENT_ID and OPE_YTMUSIC_CLIENT_SECRET are required before "
        "connecting YouTube Music with device-code auth"
    )


def _cleanup_pending_device_codes(now: float) -> None:
    expired = [
        state for state, pending in _PENDING_DEVICE_CODES.items() if pending.expires_at <= now
    ]
    for state in expired:
        _PENDING_DEVICE_CODES.pop(state, None)


def _store_device_code(state: str, pending: _PendingDeviceCode) -> None:
    _cleanup_pending_device_codes(time.time())
    _PENDING_DEVICE_CODES[state] = pending


def _get_device_code_state(state: str | None, user_id: str) -> _PendingDeviceCode:
    if not state:
        raise ProviderError("YouTube Music device auth requires state")
    pending = _PENDING_DEVICE_CODES.get(state)
    if pending is None:
        raise ProviderError("YouTube Music device code is invalid or expired")
    if pending.user_id != user_id:
        raise ProviderError("YouTube Music device code does not match the current user")
    if pending.expires_at <= time.time():
        _PENDING_DEVICE_CODES.pop(state, None)
        raise ProviderError("YouTube Music device code expired; start connection again")
    return pending


def _token_expires_at(expires_in: Any) -> int:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 0
    return int(time.time()) + max(0, seconds - 30)


def _required_token_value(token: dict[str, Any], key: str) -> str:
    value = token.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError(f"YouTube Music OAuth response did not include {key}")
    return value


def _token_payload(token: dict[str, Any]) -> dict[str, Any]:
    expires_in = token.get("expires_in")
    return {
        "access_token": _required_token_value(token, "access_token"),
        "refresh_token": _required_token_value(token, "refresh_token"),
        "scope": token.get("scope") or _YTMUSIC_SCOPE,
        "token_type": token.get("token_type") or "Bearer",
        "expires_at": _token_expires_at(expires_in),
        "expires_in": token.get("refresh_token_expires_in") or expires_in or 0,
    }


def _refresh_payload(existing: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
    access_token = _required_token_value(token, "access_token")
    return {
        **existing,
        "access_token": access_token,
        "scope": token.get("scope") or existing.get("scope") or _YTMUSIC_SCOPE,
        "token_type": token.get("token_type") or existing.get("token_type") or "Bearer",
        "expires_at": _token_expires_at(token.get("expires_in")),
    }


class YTMusicAuth(AuthStrategy):
    kind = AuthKind.OAUTH_DEVICE

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _post_form(self, url: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            resp = await client.post(url, data=data, headers={"User-Agent": _OAUTH_USER_AGENT})
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"YouTube Music OAuth returned invalid JSON (HTTP {resp.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError("YouTube Music OAuth returned an invalid response")
        return resp.status_code, payload

    async def _request_device_code(self) -> dict[str, Any]:
        s = get_settings()
        status, payload = await self._post_form(
            s.ytmusic_device_code_url,
            {"client_id": s.ytmusic_client_id, "scope": _YTMUSIC_SCOPE},
        )
        if status >= 400:
            error = payload.get("error") or payload.get("error_code") or status
            raise ProviderError(f"YouTube Music device-code request failed: {error}")
        return payload

    async def _poll_token(self, pending: _PendingDeviceCode) -> dict[str, Any]:
        s = get_settings()
        status, payload = await self._post_form(
            s.ytmusic_token_url,
            {
                "client_id": s.ytmusic_client_id,
                "client_secret": s.ytmusic_client_secret,
                "grant_type": _OAUTH_GRANT_TYPE,
                "code": pending.device_code,
            },
        )
        error = payload.get("error")
        if error == "authorization_pending":
            raise ProviderError("authorization_pending")
        if error == "slow_down":
            pending.interval_s += 5
            raise RateLimited(retry_after_s=float(pending.interval_s), message="slow_down")
        if error in {"access_denied", "expired_token"}:
            message = "denied" if error == "access_denied" else "expired"
            raise ProviderError(f"YouTube Music device authorization was {message}")
        if status >= 400 or error:
            raise ProviderError(f"YouTube Music token exchange failed: {error or status}")
        return payload

    async def _refresh_token(self, refresh_token: str) -> dict[str, Any]:
        s = get_settings()
        status, payload = await self._post_form(
            s.ytmusic_token_url,
            {
                "client_id": s.ytmusic_client_id,
                "client_secret": s.ytmusic_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if status >= 400 or payload.get("error"):
            raise AuthExpired(f"YouTube Music refresh failed: {payload.get('error') or status}")
        return payload

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        s = get_settings()
        if _has_oauth_settings(s):
            data = await self._request_device_code()
            device_code = data.get("device_code")
            user_code = data.get("user_code")
            verification_url = data.get("verification_url")
            if not all(
                isinstance(v, str) and v for v in (device_code, user_code, verification_url)
            ):
                raise ProviderError(
                    "YouTube Music device-code response was missing required fields"
                )
            interval_s = int(data.get("interval") or _DEFAULT_POLL_INTERVAL_S)
            state = uuid.uuid4().hex
            _store_device_code(
                state,
                _PendingDeviceCode(
                    user_id=user_id,
                    device_code=device_code,
                    expires_at=time.time() + int(data.get("expires_in") or 1800),
                    interval_s=interval_s,
                ),
            )
            return AuthChallenge(
                shape=ChallengeShape.DEVICE_CODE,
                user_code=user_code,
                verification_url=verification_url,
                poll_interval_s=interval_s,
                state=state,
                instructions=(
                    "Open the verification URL and enter the code to connect YouTube Music."
                ),
            )
        if _has_partial_oauth_settings(s):
            raise _oauth_settings_error()
        if s.allow_header_paste:
            return AuthChallenge(
                shape=ChallengeShape.FORM,
                instructions=(
                    "Self-host fallback: paste request headers from an authenticated "
                    "music.youtube.com session. Configure OPE_YTMUSIC_CLIENT_ID and "
                    "OPE_YTMUSIC_CLIENT_SECRET to use device-code auth."
                ),
                form_schema={"headers_raw": {"type": "string", "format": "textarea"}},
            )
        raise _oauth_settings_error()

    async def complete(self, *, user_id: str, callback: dict) -> ProviderCredential:
        headers_raw = callback.get("headers_raw")
        if headers_raw is not None:
            if not get_settings().allow_header_paste:
                raise Unsupported(
                    "YouTube Music header-paste auth is disabled outside self-host mode"
                )
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
        s = get_settings()
        if not _has_oauth_settings(s):
            raise _oauth_settings_error()
        raw_state = callback.get("state")
        state = raw_state if isinstance(raw_state, str) else None
        pending = _get_device_code_state(state, user_id)
        try:
            token = await self._poll_token(pending)
        except ProviderError as exc:
            if str(exc) in {
                "YouTube Music device authorization was denied",
                "YouTube Music device authorization was expired",
            }:
                _PENDING_DEVICE_CODES.pop(state, None)
            raise
        _PENDING_DEVICE_CODES.pop(state, None)
        auth_payload = _token_payload(token)
        return ProviderCredential(
            account_id="ytmusic-oauth",
            provider="ytmusic",
            auth_kind=AuthKind.OAUTH_DEVICE,
            access_token=auth_payload["access_token"],
            refresh_token=auth_payload["refresh_token"],
            expires_at=auth_payload["expires_at"],
            scopes=str(auth_payload["scope"]).split(),
            extra={"auth": auth_payload, "display_name": "YouTube Music"},
        )

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        if cred.auth_kind is not AuthKind.OAUTH_DEVICE:
            return cred
        s = get_settings()
        if not _has_oauth_settings(s):
            raise _oauth_settings_error()
        refresh_token = cred.refresh_token or cred.extra.get("auth", {}).get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AuthExpired("YouTube Music refresh token is missing")
        current_auth = cred.extra.get("auth")
        if not isinstance(current_auth, dict):
            current_auth = {}
        token = await self._refresh_token(refresh_token)
        auth_payload = _refresh_payload(current_auth, token)
        return cred.model_copy(
            update={
                "access_token": auth_payload["access_token"],
                "refresh_token": auth_payload.get("refresh_token") or refresh_token,
                "expires_at": auth_payload["expires_at"],
                "scopes": str(auth_payload.get("scope") or _YTMUSIC_SCOPE).split(),
                "extra": {**cred.extra, "auth": auth_payload},
            }
        )

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
        rows = await _run_client_call(
            "list playlists", lambda: client.get_library_playlists(limit=1000)
        )
        if not isinstance(rows, list):
            raise ProviderError("YouTube Music list playlists returned an invalid response")
        for item in rows:
            if not isinstance(item, dict):
                continue
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
        playlist = await _run_client_call(
            "read playlist", lambda: client.get_playlist(ref.id, limit=5000), playlist_id=ref.id
        )
        if not isinstance(playlist, dict):
            raise ProviderError("YouTube Music playlist response was invalid")
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
        raw = await _run_client_call(
            "read playlist", lambda: client.get_playlist(ref.id, limit=5000), playlist_id=ref.id
        )
        if not isinstance(raw, dict):
            raise ProviderError("YouTube Music playlist response was invalid")
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
        rows = await _run_client_call(
            "test connection", lambda: client.get_library_playlists(limit=1)
        )
        if not isinstance(rows, list):
            raise ProviderError("YouTube Music test connection returned an invalid response")

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        query = f"{track.title} {track.artist}".strip()
        if track.album:
            query = f"{query} {track.album}".strip()
        client = self._client(cred)
        results = await _run_client_call(
            "search tracks", lambda: client.search(query, filter="songs", limit=limit)
        )
        if not isinstance(results, list):
            raise ProviderError("YouTube Music search returned an invalid response")
        candidates: list[TrackCandidate] = []
        for item in results:
            if not isinstance(item, dict):
                continue
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
        result = await _run_client_call(
            "create playlist",
            lambda: client.create_playlist(spec.name, spec.description or "", privacy),
        )
        if not isinstance(result, str):
            message = str(result)
            if _is_auth_error_message(message):
                raise _auth_error("create playlist", message)
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
            response = await _run_client_call(
                "add tracks",
                partial(client.add_playlist_items, playlist_id, video_ids, None, True),
                playlist_id=playlist_id,
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
