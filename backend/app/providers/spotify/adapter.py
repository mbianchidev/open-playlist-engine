"""Spotify adapter — official read/search/write, ISRC-rich, OAuth PKCE.

Read/search talk to the Spotify Web API over ``httpx``. The HTTP transport is
injectable (``SpotifyAdapter(transport=...)``) so the conformance suite can drive
the adapter against recorded fixtures instead of the live API — never live calls
in CI. Writes use Spotify's current playlist-items and unified library endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date

import httpx

from app.core.adapter import (
    AccessDenied,
    AddItemResult,
    AlbumCandidate,
    ArtistCandidate,
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
    RefreshTokenExpired,
    TrackCandidate,
)
from app.core.capabilities import (
    Capability,
    CapabilityDescriptor,
    SearchMode,
    Stability,
)
from app.core.models import (
    Album,
    Artist,
    ArtistCollectionSemantics,
    MediaType,
    Playlist,
    PlaylistKind,
    PlaylistRef,
    Track,
)
from app.core.registry import register
from app.settings import get_settings

_API_BASE = "https://api.spotify.com/v1"
_LIST_PAGE = 50
_ITEMS_PAGE = 100
_SAVED_ITEMS_PAGE = 50
_LIBRARY_WRITE_BATCH = 40
_RATE_LIMIT_STATUSES = {420, 429}
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_MAX_AUTO_WAIT_S = 30
SPOTIFY_SAVED_TRACKS_PLAYLIST_ID = "spotify:saved-tracks"
_SAVED_TRACKS_NAME = "Liked Songs"
_SAVED_TRACKS_HREF = "/me/tracks"
_SAVED_TRACKS_SCOPE = "user-library-read"
_SAVED_TRACKS_WRITE_SCOPE = "user-library-modify"
_FOLLOWED_ARTISTS_READ_SCOPE = "user-follow-read"
_FOLLOWED_ARTISTS_WRITE_SCOPE = "user-follow-modify"
_SAVED_TRACKS_SCOPE_MESSAGE = (
    "Spotify saved songs need the user-library-read scope; reconnect Spotify to migrate "
    "saved songs."
)
_SAVED_TRACKS_WRITE_SCOPE_MESSAGE = (
    "Spotify saved songs need the user-library-modify scope; reconnect Spotify to "
    "write liked songs."
)

_SCOPES = [
    "user-read-private",
    _SAVED_TRACKS_SCOPE,
    _SAVED_TRACKS_WRITE_SCOPE,
    _FOLLOWED_ARTISTS_READ_SCOPE,
    _FOLLOWED_ARTISTS_WRITE_SCOPE,
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_STATE_TTL_S = 600
_PLAYLIST_TRACK_ACCESS_MESSAGE = (
    "Spotify does not allow this app to read tracks from playlists you do not own "
    "or collaborate on. In Spotify, use 'Add to other playlist' to copy it into a "
    "playlist you own, then migrate that copy. Delta migration is not available for "
    "the original external playlist because Spotify blocks track access."
)


@dataclass(frozen=True)
class _PendingState:
    user_id: str
    code_verifier: str
    created_at: float


@dataclass(frozen=True)
class _SavedPlaylist:
    ref: PlaylistRef
    tracks_href: str | None


_PENDING_STATES: dict[str, _PendingState] = {}
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _raise_for_status(resp: httpx.Response) -> httpx.Response:
    """Translate Spotify HTTP failures into the core's typed errors."""
    if resp.is_success:
        return resp
    if resp.status_code == 401:
        raise AuthExpired("spotify authorization expired; reconnect Spotify")
    if resp.status_code == 403:
        raise AccessDenied("spotify request forbidden")
    if resp.status_code == 404:
        raise NotFound(str(resp.request.url))
    if resp.status_code in _RATE_LIMIT_STATUSES:
        retry_after_s = _retry_after_seconds(resp)
        message = "spotify rate limited"
        if retry_after_s is not None:
            message = f"{message}; retry after {retry_after_s:g} seconds"
        raise RateLimited(
            retry_after_s=retry_after_s,
            message=message,
            status_code=resp.status_code,
        )
    raise ProviderError(f"spotify HTTP {resp.status_code}: {_spotify_error_message(resp)}")


async def _spotify_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> httpx.Response:
    retries = 0
    while True:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code not in _RATE_LIMIT_STATUSES:
            return resp
        retry_after_s = _retry_after_seconds(resp)
        if (
            retry_after_s is None
            or retry_after_s > _RATE_LIMIT_MAX_AUTO_WAIT_S
            or retries >= _RATE_LIMIT_MAX_RETRIES
        ):
            logger.warning(
                "spotify rate limited status=%s retry_after_s=%s retry=%s/%s "
                "auto_wait_cap_s=%s returning error path=%s",
                resp.status_code,
                retry_after_s,
                retries,
                _RATE_LIMIT_MAX_RETRIES,
                _RATE_LIMIT_MAX_AUTO_WAIT_S,
                resp.request.url.path,
            )
            return resp
        retries += 1
        logger.warning(
            "spotify rate limited status=%s retry_after_s=%s retry=%s/%s path=%s",
            resp.status_code,
            retry_after_s,
            retries,
            _RATE_LIMIT_MAX_RETRIES,
            resp.request.url.path,
        )
        await asyncio.sleep(retry_after_s)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    retry_after = resp.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _spotify_error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return resp.text or resp.reason_phrase
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(error, str) and error:
        return error
    return resp.reason_phrase


def _spotify_error_code(resp: httpx.Response) -> str | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) and error else None


def _raise_playlist_tracks_forbidden() -> None:
    raise AccessDenied(_PLAYLIST_TRACK_ACCESS_MESSAGE)


def _raise_saved_tracks_forbidden() -> None:
    raise AccessDenied(_SAVED_TRACKS_SCOPE_MESSAGE)


def _raise_saved_tracks_write_forbidden() -> None:
    raise AccessDenied(_SAVED_TRACKS_WRITE_SCOPE_MESSAGE)


def _spotify_user_id(cred: ProviderCredential) -> str | None:
    provider_user_id = cred.extra.get("provider_user_id")
    return provider_user_id if isinstance(provider_user_id, str) and provider_user_id else None


def _saved_tracks_snapshot(total: int | None) -> str | None:
    return f"{SPOTIFY_SAVED_TRACKS_PLAYLIST_ID}:total:{total}" if total is not None else None


def _saved_tracks_ref(
    cred: ProviderCredential, *, track_count: int | None = None, migration_note: str | None = None
) -> PlaylistRef:
    return PlaylistRef(
        id=SPOTIFY_SAVED_TRACKS_PLAYLIST_ID,
        name=_SAVED_TRACKS_NAME,
        track_count=track_count,
        owner_id=_spotify_user_id(cred),
        collaborative=False,
        snapshot_id=_saved_tracks_snapshot(track_count),
        tracks_href=_SAVED_TRACKS_HREF,
        migration_note=migration_note,
        kind=PlaylistKind.LIKED_TRACKS,
    )


def _media_type(spotify_type: str | None, is_local: bool) -> MediaType:
    if is_local:
        return MediaType.LOCAL_FILE
    return {
        "track": MediaType.TRACK,
        "episode": MediaType.EPISODE,
    }.get(spotify_type or "track", MediaType.UNKNOWN)


def _join_artists(artists: list[dict]) -> str:
    names = [a.get("name", "") for a in artists if a.get("name")]
    return ", ".join(names) or "Unknown"


def _artist_credit(artists: list[dict]) -> list[dict[str, str]]:
    credits = []
    for artist in artists:
        name = artist.get("name")
        if not name:
            continue
        credit: dict[str, str] = {"role": "artist", "name": name}
        uri = artist.get("uri") or artist.get("external_urls", {}).get("spotify")
        if uri:
            credit["uri"] = uri
        credits.append(credit)
    return credits


def _image_uri(album: dict) -> str | None:
    images = album.get("images") or []
    if not images:
        return None
    first = images[0]
    return first.get("url") if isinstance(first, dict) else None


def _release_date(album: dict) -> tuple[date | None, int | None]:
    raw = album.get("release_date")
    if not isinstance(raw, str) or not raw:
        return None, None
    year = int(raw[:4]) if len(raw) >= 4 and raw[:4].isdigit() else None
    precision = album.get("release_date_precision")
    if precision == "day":
        try:
            return date.fromisoformat(raw), year
        except ValueError:
            return None, year
    return None, year


def _playlist_item_page(payload: dict) -> dict | None:
    tracks = payload.get("tracks")
    if isinstance(tracks, dict) and isinstance(tracks.get("items"), list):
        return tracks
    items = payload.get("items")
    if isinstance(items, dict) and isinstance(items.get("items"), list):
        return items
    if isinstance(items, list):
        return payload
    return None


def _playlist_tracks_href(payload: dict) -> str | None:
    items = payload.get("items")
    if not isinstance(items, dict):
        items = payload.get("tracks")
    if not isinstance(items, dict):
        return None
    return _href_from_page(items)


def _playlist_items_summary(payload: dict) -> dict:
    items = payload.get("items")
    if isinstance(items, dict):
        return items
    tracks = payload.get("tracks")
    return tracks if isinstance(tracks, dict) else {}


def _href_from_page(page: dict) -> str | None:
    href = page.get("href")
    return href if isinstance(href, str) and href else None


def _page_needs_href_fetch(page: dict) -> bool:
    total = page.get("total")
    return (
        isinstance(total, int)
        and total > 0
        and page.get("items") == []
        and bool(page.get("href"))
    )


async def _iter_tracks_from_page(client: httpx.AsyncClient, page: dict) -> AsyncIterator[Track]:
    position = 0
    while True:
        for item in page.get("items") or []:
            track = _track_from_item(item)
            if track is None:
                continue
            track.position = position
            position += 1
            yield track
        next_url = page.get("next")
        if not next_url:
            break
        page_resp = _raise_for_status(await _spotify_request(client, "GET", next_url))
        next_page = _playlist_item_page(page_resp.json())
        if next_page is None:
            break
        page = next_page


async def _saved_playlist_ref(client: httpx.AsyncClient, playlist_id: str) -> _SavedPlaylist | None:
    offset = 0
    while True:
        resp = _raise_for_status(
            await _spotify_request(
                client, "GET", "/me/playlists", params={"limit": _LIST_PAGE, "offset": offset}
            )
        )
        data = resp.json()
        for pl in data.get("items", []):
            if pl.get("id") == playlist_id:
                tracks = _playlist_items_summary(pl)
                return _SavedPlaylist(
                    ref=PlaylistRef(
                        id=pl["id"],
                        name=pl.get("name") or "",
                        track_count=tracks.get("total"),
                        owner_id=(pl.get("owner") or {}).get("id"),
                        collaborative=pl.get("collaborative"),
                        snapshot_id=pl.get("snapshot_id"),
                        tracks_href=_href_from_page(tracks),
                    ),
                    tracks_href=_href_from_page(tracks),
                )
        if not data.get("next"):
            return None
        offset += _LIST_PAGE


async def _saved_tracks_playlist_ref(
    client: httpx.AsyncClient, cred: ProviderCredential
) -> PlaylistRef:
    if _SAVED_TRACKS_SCOPE not in cred.scopes:
        return _saved_tracks_ref(cred, migration_note="Reconnect Spotify to migrate saved songs")
    resp = await _spotify_request(
        client,
        "GET",
        _SAVED_TRACKS_HREF,
        params={"limit": 1, "offset": 0},
    )
    if resp.status_code == 403:
        logger.warning("spotify saved songs unavailable; reconnect with user-library-read scope")
        return _saved_tracks_ref(cred, migration_note="Reconnect Spotify to migrate saved songs")
    data = _raise_for_status(resp).json()
    total = data.get("total")
    return _saved_tracks_ref(cred, track_count=total if isinstance(total, int) else None)


def _track_from_item(item: dict) -> Track | None:
    """Map one Spotify playlist item to the Open Playlist model."""
    obj = item.get("track") or item.get("item")
    if not obj:  # null when a track was removed from the catalogue
        return None
    is_local = bool(item.get("is_local") or obj.get("is_local"))
    media = _media_type(obj.get("type"), is_local)
    uri = obj.get("uri")
    duration_ms = obj.get("duration_ms") or 0
    album = obj.get("album") or {}
    release_date, release_year = _release_date(album)
    track = Track(
        id=obj.get("id"),
        title=obj.get("name") or "",
        artist=_join_artists(obj.get("artists", [])),
        album=album.get("name"),
        duration_s=duration_ms // 1000 or None,
        release_date=release_date,
        release_year=release_year,
        track_number=obj.get("track_number"),
        disc_number=obj.get("disc_number"),
        explicit=obj.get("explicit"),
        credits=_artist_credit(obj.get("artists", [])),
        isrc=(obj.get("external_ids") or {}).get("isrc"),
        artwork_uri=_image_uri(album),
        provider_uris={"spotify": uri} if uri else {},
        metadata={
            key: value
            for key, value in {
                "spotify_album_id": album.get("id"),
                "spotify_popularity": obj.get("popularity"),
                "spotify_preview_url": obj.get("preview_url"),
            }.items()
            if value is not None
        },
        media_type=media,
        is_local=is_local,
        source_item_id=obj.get("id"),
        added_at=item.get("added_at"),
    )
    if not track.is_migratable:
        track.unsupported_reason = (
            "local file" if is_local else f"unsupported item type: {obj.get('type')}"
        )
    return track


def _candidate(obj: dict) -> TrackCandidate:
    track_id = obj.get("id") or ""
    return TrackCandidate(
        provider_track_id=track_id,
        uri=obj.get("uri") or f"spotify:track:{track_id}",
        title=obj.get("name") or "",
        artist=_join_artists(obj.get("artists", [])),
        album=(obj.get("album") or {}).get("name"),
        duration_s=(obj.get("duration_ms") or 0) // 1000 or None,
        isrc=(obj.get("external_ids") or {}).get("isrc"),
        explicit=obj.get("explicit"),
    )


def _track_id(uri: str) -> str | None:
    """Extract a Spotify track id from a URI, open.spotify URL, or bare id."""
    uri = uri.strip()
    if uri.startswith("spotify:track:"):
        return uri.rsplit(":", 1)[-1] or None
    if "/track/" in uri:
        tail = uri.split("/track/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0] or None
    return uri or None


def _album_id(uri: str) -> str | None:
    uri = uri.strip()
    if uri.startswith("spotify:album:"):
        return uri.rsplit(":", 1)[-1] or None
    if "/album/" in uri:
        tail = uri.split("/album/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0] or None
    return uri if uri and ":" not in uri else None


def _artist_id(uri: str) -> str | None:
    uri = uri.strip()
    if uri.startswith("spotify:artist:"):
        return uri.rsplit(":", 1)[-1] or None
    if "/artist/" in uri:
        tail = uri.split("/artist/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0] or None
    return uri if uri and ":" not in uri else None


def _album_from_object(obj: dict, *, added_at: str | None = None) -> Album:
    album_id = str(obj.get("id") or "")
    release_date, release_year = _release_date(obj)
    uri = obj.get("uri") or (f"spotify:album:{album_id}" if album_id else None)
    return Album(
        id=album_id or None,
        title=str(obj.get("name") or album_id),
        artists=[
            str(artist.get("name"))
            for artist in obj.get("artists") or []
            if isinstance(artist, dict) and artist.get("name")
        ],
        upc=(obj.get("external_ids") or {}).get("upc"),
        release_date=release_date,
        release_year=release_year,
        artwork_uri=_image_uri(obj),
        provider_uris={"spotify": uri} if uri else {},
        metadata={
            key: value
            for key, value in {
                "album_type": obj.get("album_type"),
                "total_tracks": obj.get("total_tracks"),
                "spotify_release_date": obj.get("release_date"),
                "release_date_precision": obj.get("release_date_precision"),
            }.items()
            if value is not None
        },
        source_item_id=album_id or None,
        added_at=added_at,
    )


def _artist_from_object(obj: dict, *, added_at: str | None = None) -> Artist:
    artist_id = str(obj.get("id") or "")
    uri = obj.get("uri") or (f"spotify:artist:{artist_id}" if artist_id else None)
    images = obj.get("images") or []
    artwork_uri = (
        images[0].get("url")
        if images and isinstance(images[0], dict) and images[0].get("url")
        else None
    )
    return Artist(
        id=artist_id or None,
        name=str(obj.get("name") or artist_id),
        artwork_uri=artwork_uri,
        provider_uris={"spotify": uri} if uri else {},
        metadata={
            key: value
            for key, value in {
                "genres": obj.get("genres"),
                "followers": (obj.get("followers") or {}).get("total"),
                "popularity": obj.get("popularity"),
            }.items()
            if value is not None
        },
        source_item_id=artist_id or None,
        added_at=added_at,
    )


def _album_candidate(obj: dict) -> AlbumCandidate:
    album = _album_from_object(obj)
    album_id = album.id or ""
    return AlbumCandidate(
        provider_album_id=album_id,
        uri=album.provider_uris.get("spotify") or f"spotify:album:{album_id}",
        title=album.title,
        artists=album.artists,
        upc=album.upc,
        release_date=(
            str(album.release_date)
            if album.release_date
            else str(album.release_year) if album.release_year else None
        ),
        artwork_uri=album.artwork_uri,
    )


def _artist_candidate(obj: dict) -> ArtistCandidate:
    artist = _artist_from_object(obj)
    artist_id = artist.id or ""
    return ArtistCandidate(
        provider_artist_id=artist_id,
        uri=artist.provider_uris.get("spotify") or f"spotify:artist:{artist_id}",
        name=artist.name,
        artwork_uri=artist.artwork_uri,
    )


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _store_state(state: str, pending: _PendingState) -> None:
    now = time.time()
    expired = [
        key for key, value in _PENDING_STATES.items() if now - value.created_at > _STATE_TTL_S
    ]
    for key in expired:
        _PENDING_STATES.pop(key, None)
    _PENDING_STATES[state] = pending


def _consume_state(state: str | None, user_id: str) -> _PendingState:
    if not state:
        raise ProviderError("spotify callback is missing state")
    pending = _PENDING_STATES.pop(state, None)
    if pending is None:
        raise ProviderError("spotify callback state is invalid or expired")
    if pending.user_id != user_id:
        raise ProviderError("spotify callback state does not match the current user")
    return pending


def _expires_at(expires_in: int | None) -> float | None:
    if not expires_in:
        return None
    return time.time() + max(0, expires_in - 30)


def _token_auth(settings) -> tuple[str, str] | None:
    if settings.spotify_client_secret:
        return (settings.spotify_client_id, settings.spotify_client_secret)
    return None


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class SpotifyAuth(AuthStrategy):
    kind = AuthKind.OAUTH_PKCE

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        s = get_settings()
        if not s.spotify_client_id:
            raise ProviderError("OPE_SPOTIFY_CLIENT_ID is required before connecting Spotify")
        state = uuid.uuid4().hex
        code_verifier = secrets.token_urlsafe(64)
        _store_state(
            state,
            _PendingState(user_id=user_id, code_verifier=code_verifier, created_at=time.time()),
        )
        params = {
            "client_id": s.spotify_client_id,
            "response_type": "code",
            "redirect_uri": s.spotify_redirect_uri,
            "scope": " ".join(_SCOPES),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": _code_challenge(code_verifier),
        }
        url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)
        return AuthChallenge(shape=ChallengeShape.REDIRECT, redirect_url=url, state=state)

    async def complete(self, *, user_id: str, callback: dict) -> ProviderCredential:
        if callback.get("error"):
            raise ProviderError(f"spotify authorization failed: {callback['error']}")
        code = callback.get("code")
        if not code:
            raise ProviderError("spotify callback is missing code")
        pending = _consume_state(callback.get("state"), user_id)
        s = get_settings()
        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            resp = await _spotify_request(
                client,
                "POST",
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": s.spotify_redirect_uri,
                    "client_id": s.spotify_client_id,
                    "code_verifier": pending.code_verifier,
                },
                auth=_token_auth(s),
            )
            if not resp.is_success:
                _raise_for_status(resp)
                raise ProviderError(f"spotify token exchange failed with HTTP {resp.status_code}")
            token = resp.json()
            access_token = token.get("access_token")
            if not access_token:
                raise ProviderError("spotify token response did not include an access token")
            profile_resp = await _spotify_request(
                client,
                "GET",
                f"{_API_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            _raise_for_status(profile_resp)
        profile = profile_resp.json()
        provider_user_id = profile.get("id")
        if not provider_user_id:
            raise ProviderError("spotify profile response did not include a user id")
        return ProviderCredential(
            account_id=provider_user_id,
            provider="spotify",
            auth_kind=self.kind,
            access_token=access_token,
            refresh_token=token.get("refresh_token"),
            expires_at=_expires_at(token.get("expires_in")),
            scopes=(token.get("scope") or " ".join(_SCOPES)).split(),
            extra={
                "display_name": profile.get("display_name") or provider_user_id,
                "provider_user_id": provider_user_id,
            },
        )

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        if not cred.refresh_token:
            raise RefreshTokenExpired("spotify refresh token is missing; reconnect Spotify")
        s = get_settings()
        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            resp = await _spotify_request(
                client,
                "POST",
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": cred.refresh_token,
                    "client_id": s.spotify_client_id,
                },
                auth=_token_auth(s),
            )
        if not resp.is_success:
            if resp.status_code == 400 and _spotify_error_code(resp) == "invalid_grant":
                raise RefreshTokenExpired("spotify refresh token expired; reconnect Spotify")
            _raise_for_status(resp)
            raise AuthExpired("spotify authorization expired; reconnect Spotify")
        token = resp.json()
        access_token = token.get("access_token")
        if not access_token:
            raise AuthExpired("spotify refresh response did not include an access token")
        return cred.model_copy(
            update={
                "access_token": access_token,
                "refresh_token": token.get("refresh_token") or cred.refresh_token,
                "expires_at": _expires_at(token.get("expires_in")),
                "scopes": (token.get("scope") or " ".join(cred.scopes)).split(),
            }
        )

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class SpotifyAdapter:
    info = ProviderInfo(
        name="spotify",
        display_name="Spotify",
        auth_kind=AuthKind.OAUTH_PKCE,
        capabilities=CapabilityDescriptor(
            capabilities={
                Capability.READ_PLAYLISTS,
                Capability.READ_TRACKS,
                Capability.READ_LIBRARY,
                Capability.WRITE_LIBRARY,
                Capability.READ_SAVED_ALBUMS,
                Capability.WRITE_SAVED_ALBUMS,
                Capability.READ_FOLLOWED_ARTISTS,
                Capability.WRITE_FOLLOWED_ARTISTS,
                Capability.CREATE_PLAYLIST,
                Capability.ADD_TRACKS,
                Capability.SET_DESCRIPTION,
            },
            has_isrc=True,
            search_modes=[SearchMode.ISRC, SearchMode.TEXT],
            official=True,
            stability=Stability.STABLE,
            max_add_batch=100,
            max_library_batch=_LIBRARY_WRITE_BATCH,
            max_playlist_size=10_000,
        ),
        liked_tracks_playlist_id=SPOTIFY_SAVED_TRACKS_PLAYLIST_ID,
        library_read_scope=_SAVED_TRACKS_SCOPE,
        library_write_scope=_SAVED_TRACKS_WRITE_SCOPE,
        saved_albums_read_scope=_SAVED_TRACKS_SCOPE,
        saved_albums_write_scope=_SAVED_TRACKS_WRITE_SCOPE,
        followed_artists_read_scope=_FOLLOWED_ARTISTS_READ_SCOPE,
        followed_artists_write_scope=_FOLLOWED_ARTISTS_WRITE_SCOPE,
        artist_collection_semantics=ArtistCollectionSemantics.FOLLOW,
    )
    auth = SpotifyAuth()

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Injecting a transport lets the conformance suite serve recorded fixtures.
        self._transport = transport

    def _client(self, cred: ProviderCredential) -> httpx.AsyncClient:
        if not cred.access_token:
            raise AuthExpired("missing spotify access token")
        return httpx.AsyncClient(
            base_url=_API_BASE,
            transport=self._transport,
            headers={"Authorization": f"Bearer {cred.access_token}"},
            timeout=30.0,
        )

    # READ ------------------------------------------------------------------ #
    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        offset = 0
        async with self._client(cred) as client:
            while True:
                resp = _raise_for_status(
                    await _spotify_request(
                        client,
                        "GET",
                        "/me/playlists", params={"limit": _LIST_PAGE, "offset": offset}
                    )
                )
                data = resp.json()
                for pl in data.get("items", []):
                    items = _playlist_items_summary(pl)
                    yield PlaylistRef(
                        id=pl["id"],
                        name=pl.get("name") or "",
                        track_count=items.get("total"),
                        owner_id=(pl.get("owner") or {}).get("id"),
                        collaborative=pl.get("collaborative"),
                        snapshot_id=pl.get("snapshot_id"),
                        tracks_href=_href_from_page(items),
                    )
                if not data.get("next"):
                    break
                offset += _LIST_PAGE
            yield await _saved_tracks_playlist_ref(client, cred)

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        async with self._client(cred) as client:
            if ref.id == SPOTIFY_SAVED_TRACKS_PLAYLIST_ID:
                async for track in self._iter_saved_tracks(client):
                    yield track
                return
            resp = await self._playlist_tracks_response(client, ref.id)
            if resp.status_code in {400, 403, 404}:
                meta_resp = _raise_for_status(
                    await _spotify_request(client, "GET", f"/playlists/{ref.id}")
                )
                page = _playlist_item_page(meta_resp.json())
                if page is None:
                    if resp.status_code == 403:
                        _raise_playlist_tracks_forbidden()
                    _raise_for_status(resp)
                    return
                async for track in _iter_tracks_from_page(client, page):
                    yield track
                return
            page = _playlist_item_page(_raise_for_status(resp).json())
            if page is None:
                return
            async for track in _iter_tracks_from_page(client, page):
                yield track

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        async with self._client(cred) as client:
            if ref.id == SPOTIFY_SAVED_TRACKS_PLAYLIST_ID:
                tracks, total = await self._read_saved_tracks(client)
                return Playlist(
                    id=SPOTIFY_SAVED_TRACKS_PLAYLIST_ID,
                    name=_SAVED_TRACKS_NAME,
                    owner_id=_spotify_user_id(cred),
                    snapshot_id=_saved_tracks_snapshot(total),
                    tracks=tracks,
                    kind=PlaylistKind.LIKED_TRACKS,
                )
            resp = await _spotify_request(client, "GET", f"/playlists/{ref.id}")
            if resp.status_code in {400, 403, 404}:
                fallback = await self._read_saved_playlist(client, resp, ref)
                if fallback is not None:
                    return fallback
            meta = _raise_for_status(resp).json()
            page = _playlist_item_page(meta)
            if page is not None and not _page_needs_href_fetch(page):
                tracks = [track async for track in _iter_tracks_from_page(client, page)]
            else:
                tracks = await self._read_playlist_tracks(
                    client,
                    ref.id,
                    tracks_href=_playlist_tracks_href(meta) or _href_from_page(page or {}),
                )
        return Playlist(
            id=meta.get("id") or ref.id,
            name=meta.get("name") or ref.name,
            description=meta.get("description"),
            owner_id=(meta.get("owner") or {}).get("id"),
            snapshot_id=meta.get("snapshot_id") or ref.snapshot_id,
            tracks=tracks,
        )

    async def _read_saved_playlist(
        self, client: httpx.AsyncClient, original_resp: httpx.Response, ref: PlaylistRef
    ) -> Playlist | None:
        saved = await _saved_playlist_ref(client, ref.id)
        if saved is None:
            if original_resp.status_code == 403:
                _raise_playlist_tracks_forbidden()
            _raise_for_status(original_resp)
            return None
        tracks = await self._read_playlist_tracks(
            client,
            ref.id,
            tracks_href=saved.tracks_href,
            expected_total=saved.ref.track_count,
        )
        return Playlist(
            id=saved.ref.id,
            name=saved.ref.name,
            owner_id=saved.ref.owner_id,
            snapshot_id=saved.ref.snapshot_id,
            tracks=tracks,
        )

    async def _read_playlist_tracks(
        self,
        client: httpx.AsyncClient,
        playlist_id: str,
        *,
        tracks_href: str | None = None,
        expected_total: int | None = None,
    ) -> list[Track]:
        resp = await self._playlist_tracks_response(client, playlist_id, tracks_href=tracks_href)
        if resp.status_code == 403:
            _raise_playlist_tracks_forbidden()
        _raise_for_status(resp)
        page = _playlist_item_page(resp.json())
        if page is None:
            if expected_total:
                raise ProviderError("spotify playlist tracks response did not include track items")
            return []
        return [track async for track in _iter_tracks_from_page(client, page)]

    async def _iter_saved_tracks(self, client: httpx.AsyncClient) -> AsyncIterator[Track]:
        resp = await self._saved_tracks_response(client)
        if resp.status_code == 403:
            _raise_saved_tracks_forbidden()
        page = _playlist_item_page(_raise_for_status(resp).json())
        if page is None:
            return
        async for track in _iter_tracks_from_page(client, page):
            yield track

    async def _read_saved_tracks(self, client: httpx.AsyncClient) -> tuple[list[Track], int | None]:
        resp = await self._saved_tracks_response(client)
        if resp.status_code == 403:
            _raise_saved_tracks_forbidden()
        data = _raise_for_status(resp).json()
        page = _playlist_item_page(data)
        if page is None:
            return [], data.get("total") if isinstance(data.get("total"), int) else None
        total = page.get("total")
        return [track async for track in _iter_tracks_from_page(client, page)], (
            total if isinstance(total, int) else None
        )

    async def _saved_tracks_response(self, client: httpx.AsyncClient) -> httpx.Response:
        return await _spotify_request(
            client,
            "GET",
            _SAVED_TRACKS_HREF,
            params={"limit": _SAVED_ITEMS_PAGE, "offset": 0},
        )

    async def _playlist_tracks_response(
        self,
        client: httpx.AsyncClient,
        playlist_id: str,
        *,
        tracks_href: str | None = None,
    ) -> httpx.Response:
        url = tracks_href or f"/playlists/{playlist_id}/items"
        if "?" in url:
            return await _spotify_request(client, "GET", url)
        return await _spotify_request(
            client,
            "GET",
            url,
            params={
                "limit": _ITEMS_PAGE,
                "offset": 0,
                "additional_types": "track,episode",
            },
        )

    async def test_connection(self, cred: ProviderCredential) -> None:
        async with self._client(cred) as client:
            _raise_for_status(await _spotify_request(client, "GET", "/me"))

    async def iter_saved_albums(self, cred: ProviderCredential) -> AsyncIterator[Album]:
        self.info.require_saved_albums_source(cred)
        async with self._client(cred) as client:
            url = "/me/albums"
            params: dict[str, int] | None = {"limit": _SAVED_ITEMS_PAGE, "offset": 0}
            while url:
                resp = _raise_for_status(await _spotify_request(client, "GET", url, params=params))
                payload = resp.json()
                for item in payload.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    obj = item.get("album")
                    if isinstance(obj, dict):
                        yield _album_from_object(obj, added_at=item.get("added_at"))
                next_url = payload.get("next")
                url = next_url if isinstance(next_url, str) else ""
                params = None

    async def read_saved_album(self, cred: ProviderCredential, album_id: str) -> Album:
        self.info.require_saved_albums_source(cred)
        normalized = _album_id(album_id)
        if not normalized:
            raise NotFound(album_id)
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _spotify_request(client, "GET", f"/albums/{normalized}")
            )
        return _album_from_object(resp.json())

    async def iter_followed_artists(self, cred: ProviderCredential) -> AsyncIterator[Artist]:
        self.info.require_followed_artists_source(cred)
        async with self._client(cred) as client:
            url = "/me/following"
            params: dict[str, object] | None = {"type": "artist", "limit": _SAVED_ITEMS_PAGE}
            while url:
                resp = _raise_for_status(await _spotify_request(client, "GET", url, params=params))
                page = resp.json().get("artists") or {}
                for obj in page.get("items") or []:
                    if isinstance(obj, dict):
                        yield _artist_from_object(obj)
                next_url = page.get("next")
                url = next_url if isinstance(next_url, str) else ""
                params = None

    async def read_followed_artist(self, cred: ProviderCredential, artist_id: str) -> Artist:
        self.info.require_followed_artists_source(cred)
        normalized = _artist_id(artist_id)
        if not normalized:
            raise NotFound(artist_id)
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _spotify_request(client, "GET", f"/artists/{normalized}")
            )
        return _artist_from_object(resp.json())

    # SEARCH ---------------------------------------------------------------- #
    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        if track.isrc:
            query = f"isrc:{track.isrc}"
        else:
            query = f'track:"{track.title}"'
            if track.artist:
                query += f' artist:"{track.artist}"'
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _spotify_request(
                    client,
                    "GET",
                    "/search", params={"q": query, "type": "track", "limit": limit}
                )
            )
        items = ((resp.json().get("tracks") or {}).get("items")) or []
        return [_candidate(obj) for obj in items[:limit]]

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        track_id = _track_id(uri)
        if not track_id:
            return False
        async with self._client(cred) as client:
            resp = await _spotify_request(client, "GET", f"/tracks/{track_id}")
        if resp.status_code == 404:
            return False
        _raise_for_status(resp)
        return True

    async def search_albums(
        self, cred: ProviderCredential, album: Album, *, limit: int = 5
    ) -> list[AlbumCandidate]:
        queries = []
        if album.upc:
            queries.append(f"upc:{album.upc}")
        query = f'album:"{album.title}"'
        if album.artists:
            query += f' artist:"{album.artists[0]}"'
        queries.append(query)
        async with self._client(cred) as client:
            for search_query in queries:
                resp = _raise_for_status(
                    await _spotify_request(
                        client,
                        "GET",
                        "/search",
                        params={"q": search_query, "type": "album", "limit": min(limit, 10)},
                    )
                )
                items = ((resp.json().get("albums") or {}).get("items")) or []
                if items:
                    return [_album_candidate(obj) for obj in items[:limit]]
        return []

    async def search_artists(
        self, cred: ProviderCredential, artist: Artist, *, limit: int = 5
    ) -> list[ArtistCandidate]:
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _spotify_request(
                    client,
                    "GET",
                    "/search",
                    params={
                        "q": f'artist:"{artist.name}"',
                        "type": "artist",
                        "limit": min(limit, 10),
                    },
                )
            )
        items = ((resp.json().get("artists") or {}).get("items")) or []
        return [_artist_candidate(obj) for obj in items[:limit]]

    async def validate_album_uri(self, cred: ProviderCredential, uri: str) -> bool:
        album_id = _album_id(uri)
        if not album_id:
            return False
        async with self._client(cred) as client:
            resp = await _spotify_request(client, "GET", f"/albums/{album_id}")
        if resp.status_code == 404:
            return False
        _raise_for_status(resp)
        return True

    async def validate_artist_uri(self, cred: ProviderCredential, uri: str) -> bool:
        artist_id = _artist_id(uri)
        if not artist_id:
            return False
        async with self._client(cred) as client:
            resp = await _spotify_request(client, "GET", f"/artists/{artist_id}")
        if resp.status_code == 404:
            return False
        _raise_for_status(resp)
        return True

    # WRITE ----------------------------------------------------------------- #
    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _spotify_request(
                    client,
                    "POST",
                    "/me/playlists",
                    json={
                        "name": spec.name,
                        "description": spec.description or "",
                        "public": spec.public,
                    },
                )
            )
        playlist_id = resp.json().get("id")
        if not isinstance(playlist_id, str) or not playlist_id:
            raise ProviderError("spotify create playlist response did not include playlist id")
        return playlist_id

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        if playlist_id == SPOTIFY_SAVED_TRACKS_PLAYLIST_ID:
            return await self._save_library_tracks(cred, uris)
        return await self._add_playlist_tracks(cred, playlist_id, uris)

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        self.info.require_saved_albums_source(cred)
        return await self._contains_library_items(cred, uris, _album_id, "album")

    async def contains_followed_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        self.info.require_followed_artists_source(cred)
        return await self._contains_library_items(cred, uris, _artist_id, "artist")

    async def save_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self.info.require_saved_albums_target(cred)
        return await self._write_library_uris(cred, uris, _album_id, "album")

    async def follow_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self.info.require_followed_artists_target(cred)
        return await self._write_library_uris(cred, uris, _artist_id, "artist")

    async def _contains_library_items(
        self,
        cred: ProviderCredential,
        uris: Sequence[str],
        normalizer,
        item_type: str,
    ) -> list[bool]:
        originals = list(uris)
        normalized = [
            f"spotify:{item_type}:{item_id}" if (item_id := normalizer(uri)) else None
            for uri in originals
        ]
        results = [False] * len(originals)
        valid = [(index, uri) for index, uri in enumerate(normalized) if uri]
        async with self._client(cred) as client:
            for start in range(0, len(valid), self.info.capabilities.max_library_batch):
                chunk = valid[start : start + self.info.capabilities.max_library_batch]
                resp = _raise_for_status(
                    await _spotify_request(
                        client,
                        "GET",
                        "/me/library/contains",
                        params={"uris": ",".join(uri for _, uri in chunk)},
                    )
                )
                payload = resp.json()
                values = payload.get("contains") if isinstance(payload, dict) else payload
                if not isinstance(values, list):
                    raise ProviderError("spotify library contains response was not a list")
                for (index, _), present in zip(chunk, values, strict=False):
                    results[index] = bool(present)
        return results

    async def _write_library_uris(
        self,
        cred: ProviderCredential,
        uris: Sequence[str],
        normalizer,
        item_type: str,
    ) -> list[AddItemResult]:
        return await self._write_uris(
            cred,
            uris,
            normalize=lambda uri: (
                f"spotify:{item_type}:{item_id}" if (item_id := normalizer(uri)) else None
            ),
            invalid_error=f"invalid Spotify {item_type} URI",
            batch_size=self.info.capabilities.max_library_batch,
            method="PUT",
            url="/me/library",
            query_param=True,
        )

    async def _save_library_tracks(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        if _SAVED_TRACKS_WRITE_SCOPE not in cred.scopes:
            _raise_saved_tracks_write_forbidden()
        return await self._write_track_uris(
            cred,
            uris,
            batch_size=_LIBRARY_WRITE_BATCH,
            method="PUT",
            url="/me/library",
            query_param=True,
            scope_error=_raise_saved_tracks_write_forbidden,
        )

    async def _add_playlist_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        return await self._write_track_uris(
            cred,
            uris,
            batch_size=self.info.capabilities.max_add_batch,
            method="POST",
            url=f"/playlists/{playlist_id}/items",
        )

    async def _write_track_uris(
        self,
        cred: ProviderCredential,
        uris: Sequence[str],
        *,
        batch_size: int,
        method: str,
        url: str,
        query_param: bool = False,
        scope_error=None,
    ) -> list[AddItemResult]:
        return await self._write_uris(
            cred,
            uris,
            normalize=lambda uri: (
                f"spotify:track:{track_id}" if (track_id := _track_id(uri)) else None
            ),
            invalid_error="invalid Spotify track URI",
            batch_size=batch_size,
            method=method,
            url=url,
            query_param=query_param,
            scope_error=scope_error,
        )

    async def _write_uris(
        self,
        cred: ProviderCredential,
        uris: Sequence[str],
        *,
        normalize,
        invalid_error: str,
        batch_size: int,
        method: str,
        url: str,
        query_param: bool = False,
        scope_error=None,
    ) -> list[AddItemResult]:
        originals = list(uris)
        normalized = [normalize(uri) for uri in originals]
        results: list[AddItemResult | None] = [None] * len(originals)
        valid = [(index, uri) for index, uri in enumerate(normalized) if uri]
        for index, uri in enumerate(normalized):
            if uri is None:
                results[index] = AddItemResult(
                    uri=originals[index],
                    ok=False,
                    error=invalid_error,
                )

        position = 0
        async with self._client(cred) as client:
            for start in range(0, len(valid), batch_size):
                chunk = valid[start : start + batch_size]
                chunk_uris = [uri for _, uri in chunk]
                kwargs = (
                    {"params": {"uris": ",".join(chunk_uris)}}
                    if query_param
                    else {"json": {"uris": chunk_uris}}
                )
                resp = await _spotify_request(client, method, url, **kwargs)
                if resp.status_code == 403 and scope_error is not None:
                    scope_error()
                _raise_for_status(resp)
                for index, _ in chunk:
                    results[index] = AddItemResult(
                        uri=originals[index],
                        ok=True,
                        position=position,
                    )
                    position += 1
        return [
            result
            if result is not None
            else AddItemResult(uri=originals[index], ok=False, error="spotify write failed")
            for index, result in enumerate(results)
        ]


adapter = register(SpotifyAdapter())
