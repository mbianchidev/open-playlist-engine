"""Spotify adapter — read + search (live), write (stubbed), ISRC-rich, OAuth PKCE.

Read/search talk to the Spotify Web API over ``httpx``. The HTTP transport is
injectable (``SpotifyAdapter(transport=...)``) so the conformance suite can drive
the adapter against recorded fixtures instead of the live API — never live calls
in CI. Write primitives remain stubbed for a later PR; the capability descriptor
still advertises the provider's full intended surface for the UI matrix.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date

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
)
from app.core.capabilities import (
    Capability,
    CapabilityDescriptor,
    SearchMode,
    Stability,
)
from app.core.models import MediaType, Playlist, PlaylistRef, Track
from app.core.registry import register
from app.settings import get_settings

_API_BASE = "https://api.spotify.com/v1"
_LIST_PAGE = 50
_ITEMS_PAGE = 100

_SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_STATE_TTL_S = 600


@dataclass(frozen=True)
class _PendingState:
    user_id: str
    code_verifier: str
    created_at: float


_PENDING_STATES: dict[str, _PendingState] = {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _raise_for_status(resp: httpx.Response) -> httpx.Response:
    """Translate Spotify HTTP failures into the core's typed errors."""
    if resp.is_success:
        return resp
    if resp.status_code == 401:
        raise AuthExpired("spotify access token expired")
    if resp.status_code == 403:
        raise ProviderError("spotify request forbidden (insufficient scope?)")
    if resp.status_code == 404:
        raise NotFound(str(resp.request.url))
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        raise RateLimited(retry_after_s=float(retry_after) if retry_after else None)
    raise ProviderError(f"spotify HTTP {resp.status_code}")


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


def _track_from_item(item: dict) -> Track | None:
    """Map one ``/playlists/{id}/tracks`` item to the Open Playlist model."""
    obj = item.get("track")
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
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
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
                raise ProviderError(f"spotify token exchange failed with HTTP {resp.status_code}")
            token = resp.json()
            access_token = token.get("access_token")
            if not access_token:
                raise ProviderError("spotify token response did not include an access token")
            profile_resp = await client.get(
                f"{_API_BASE}/me", headers={"Authorization": f"Bearer {access_token}"}
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
            extra={"display_name": profile.get("display_name") or provider_user_id},
        )

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        if not cred.refresh_token:
            raise AuthExpired("spotify refresh token is missing")
        s = get_settings()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": cred.refresh_token,
                    "client_id": s.spotify_client_id,
                },
                auth=_token_auth(s),
            )
        if not resp.is_success:
            raise AuthExpired(f"spotify refresh failed with HTTP {resp.status_code}")
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
            },
            has_isrc=True,
            search_modes=[SearchMode.ISRC, SearchMode.TEXT],
            official=True,
            stability=Stability.STABLE,
            max_add_batch=100,
            max_playlist_size=10_000,
        ),
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
                    await client.get(
                        "/me/playlists", params={"limit": _LIST_PAGE, "offset": offset}
                    )
                )
                data = resp.json()
                for pl in data.get("items", []):
                    yield PlaylistRef(
                        id=pl["id"],
                        name=pl.get("name") or "",
                        track_count=(pl.get("tracks") or {}).get("total"),
                        owner_id=(pl.get("owner") or {}).get("id"),
                    )
                if not data.get("next"):
                    break
                offset += _LIST_PAGE

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        offset = 0
        position = 0
        async with self._client(cred) as client:
            while True:
                resp = _raise_for_status(
                    await client.get(
                        f"/playlists/{ref.id}/tracks",
                        params={
                            "limit": _ITEMS_PAGE,
                            "offset": offset,
                            "additional_types": "track,episode",
                        },
                    )
                )
                data = resp.json()
                items = data.get("items", [])
                for item in items:
                    track = _track_from_item(item)
                    if track is None:
                        continue
                    track.position = position
                    position += 1
                    yield track
                if not data.get("next"):
                    break
                offset += _ITEMS_PAGE

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await client.get(
                    f"/playlists/{ref.id}",
                    params={"fields": "id,name,description,owner(id)"},
                )
            )
        meta = resp.json()
        tracks = [t async for t in self.iter_playlist_items(cred, ref)]
        return Playlist(
            id=meta.get("id") or ref.id,
            name=meta.get("name") or ref.name,
            description=meta.get("description"),
            owner_id=(meta.get("owner") or {}).get("id"),
            tracks=tracks,
        )

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
                await client.get(
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
            resp = await client.get(f"/tracks/{track_id}")
        if resp.status_code == 404:
            return False
        _raise_for_status(resp)
        return True

    # WRITE (TODO — out of scope for this PR) ------------------------------- #
    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        raise NotImplementedError("TODO: POST /users/{id}/playlists")

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        raise NotImplementedError("TODO: POST /playlists/{id}/tracks (batch <=100)")


adapter = register(SpotifyAdapter())
