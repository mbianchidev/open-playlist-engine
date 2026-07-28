"""Apple Music adapter over the official Apple Music API and MusicKit auth."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import jwt

from app.core.adapter import (
    AccessDenied,
    AddItemResult,
    AuthChallenge,
    AuthExpired,
    AuthKind,
    AuthStrategy,
    ChallengeShape,
    CreatePlaylistSpec,
    NotFound,
    PlaylistMutationResult,
    ProviderCredential,
    ProviderError,
    ProviderInfo,
    RateLimited,
    RemoveTracksResult,
    TrackCandidate,
    TrackRemoval,
    Unsupported,
)
from app.core.capabilities import (
    Capability,
    CapabilityDescriptor,
    OrderingGuarantee,
    SearchMode,
    Stability,
)
from app.core.models import MediaType, Playlist, PlaylistRef, Track
from app.core.registry import register
from app.settings import get_settings

_API_ORIGIN = "https://api.music.apple.com"
_MUSICKIT_JS_URL = "https://js-cdn.music.apple.com/musickit/v3/musickit.js"
_ACCOUNT_ID = "applemusic-user"
_LIST_PAGE = 100
_CATALOG_BATCH = 100
_MAX_ADD_BATCH = 50
_MAX_DEVELOPER_TOKEN_TTL_S = 15_777_000
_MIN_DEVELOPER_TOKEN_TTL_S = 120
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_MAX_AUTO_WAIT_S = 30
_NEW_PLAYLIST_WINDOW_S = 120
_NEW_PLAYLIST_RETRY_DELAYS_S = (1, 2, 4, 8, 15, 30)

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class _CachedDeveloperToken:
    token: str
    refresh_at: float


class AppleDeveloperTokenProvider:
    def __init__(self, *, token: str | None = None) -> None:
        self._fixed_token = token
        self._cached: _CachedDeveloperToken | None = None

    def get(self) -> str:
        if self._fixed_token:
            return self._fixed_token
        now = time.time()
        if self._cached is not None and self._cached.refresh_at > now:
            return self._cached.token

        settings = get_settings()
        if not settings.apple_music_team_id:
            raise ProviderError(
                "OPE_APPLE_MUSIC_TEAM_ID is required before connecting Apple Music"
            )
        if not settings.apple_music_key_id:
            raise ProviderError(
                "OPE_APPLE_MUSIC_KEY_ID is required before connecting Apple Music"
            )
        ttl_s = settings.apple_music_token_ttl_s
        if not _MIN_DEVELOPER_TOKEN_TTL_S <= ttl_s <= _MAX_DEVELOPER_TOKEN_TTL_S:
            raise ProviderError(
                "OPE_APPLE_MUSIC_TOKEN_TTL_S must be between "
                f"{_MIN_DEVELOPER_TOKEN_TTL_S} and {_MAX_DEVELOPER_TOKEN_TTL_S}"
            )
        private_key = _private_key(settings)
        issued_at = int(now) - 5
        expires_at = issued_at + ttl_s
        try:
            token = jwt.encode(
                {
                    "iss": settings.apple_music_team_id,
                    "iat": issued_at,
                    "exp": expires_at,
                },
                private_key,
                algorithm="ES256",
                headers={"kid": settings.apple_music_key_id},
            )
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise ProviderError(f"could not generate Apple Music developer token: {exc}") from exc
        self._cached = _CachedDeveloperToken(
            token=token,
            refresh_at=max(now, expires_at - 60),
        )
        return token


def _private_key(settings: Any) -> str:
    inline = settings.apple_music_private_key.strip()
    if inline:
        return inline.replace("\\n", "\n")
    raw_path = settings.apple_music_private_key_path.strip()
    if not raw_path:
        raise ProviderError(
            "set OPE_APPLE_MUSIC_PRIVATE_KEY or OPE_APPLE_MUSIC_PRIVATE_KEY_PATH "
            "before connecting Apple Music"
        )
    path = pathlib.Path(raw_path).expanduser()
    try:
        return path.read_text().strip()
    except OSError as exc:
        raise ProviderError(f"could not read Apple Music private key at {path}: {exc}") from exc


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    retry_after = resp.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _apple_error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return resp.text or resp.reason_phrase
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                for key in ("detail", "title", "code"):
                    value = first.get(key)
                    if isinstance(value, str) and value:
                        return value
        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return resp.reason_phrase


def _raise_for_status(resp: httpx.Response) -> httpx.Response:
    if resp.is_success:
        return resp
    if resp.status_code == 401:
        raise AuthExpired(
            "Apple Music authorization expired or the developer token is invalid; "
            "reconnect Apple Music"
        )
    if resp.status_code == 403:
        raise AccessDenied(
            "Apple Music access denied; confirm the account has an Apple Music "
            "subscription and accepted the Media & Apple Music privacy terms"
        )
    if resp.status_code == 404:
        raise NotFound(str(resp.request.url))
    if resp.status_code == 429:
        retry_after_s = _retry_after_seconds(resp)
        message = "Apple Music rate limited"
        if retry_after_s is not None:
            message = f"{message}; retry after {retry_after_s:g} seconds"
        raise RateLimited(
            retry_after_s=retry_after_s,
            message=message,
            status_code=resp.status_code,
        )
    raise ProviderError(
        f"Apple Music HTTP {resp.status_code}: {_apple_error_message(resp)}"
    )


async def _apple_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    retries = 0
    while True:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        retry_after_s = _retry_after_seconds(resp)
        if (
            retry_after_s is None
            or retry_after_s > _RATE_LIMIT_MAX_AUTO_WAIT_S
            or retries >= _RATE_LIMIT_MAX_RETRIES
        ):
            return resp
        retries += 1
        logger.warning(
            "Apple Music rate limited retry_after_s=%s retry=%s/%s path=%s",
            retry_after_s,
            retries,
            _RATE_LIMIT_MAX_RETRIES,
            resp.request.url.path,
        )
        await asyncio.sleep(retry_after_s)


def _headers(developer_token: str, music_user_token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + developer_token,
    }
    if music_user_token:
        headers["Music-User-Token"] = music_user_token
    return headers


def _attributes(resource: dict[str, Any] | None) -> dict[str, Any]:
    attrs = (resource or {}).get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict):
        return [data]
    if not isinstance(data, list):
        return []
    return [resource for resource in data if isinstance(resource, dict)]


def _next_url(payload: dict[str, Any]) -> str | None:
    value = payload.get("next")
    return value if isinstance(value, str) and value else None


def _description(attrs: dict[str, Any]) -> str | None:
    value = attrs.get("description")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("standard", "short"):
            text = value.get(key)
            if isinstance(text, str):
                return text
    return None


def _artwork_uri(attrs: dict[str, Any]) -> str | None:
    artwork = attrs.get("artwork")
    if not isinstance(artwork, dict):
        return None
    value = artwork.get("url")
    if not isinstance(value, str) or not value:
        return None
    return value.replace("{w}", "600").replace("{h}", "600").replace("{f}", "jpg")


def _release_date(raw: Any) -> tuple[date | None, int | None]:
    if not isinstance(raw, str) or not raw:
        return None, None
    year = int(raw[:4]) if len(raw) >= 4 and raw[:4].isdigit() else None
    try:
        return date.fromisoformat(raw), year
    except ValueError:
        return None, year


def _duration_s(raw: Any) -> int | None:
    if not isinstance(raw, int) or raw < 0:
        return None
    return round(raw / 1000)


def _is_explicit(attrs: dict[str, Any]) -> bool | None:
    value = attrs.get("contentRating")
    if not isinstance(value, str):
        return None
    return value.lower() == "explicit"


def _first_explicit(*attributes: dict[str, Any]) -> bool | None:
    for attrs in attributes:
        value = _is_explicit(attrs)
        if value is not None:
            return value
    return None


def _storefront(cred: ProviderCredential) -> str | None:
    value = cred.extra.get("storefront")
    return value.lower() if isinstance(value, str) and value else None


def _music_user_token(cred: ProviderCredential) -> str:
    if not cred.access_token:
        raise AuthExpired("missing Apple Music user token; reconnect Apple Music")
    return cred.access_token


def _catalog_uri(storefront: str, song_id: str) -> str:
    return f"applemusic:catalog:{storefront}:song:{song_id}"


def _library_uri(song_id: str) -> str:
    return f"applemusic:library:song:{song_id}"


def _catalog_song_from_uri(uri: str) -> tuple[str | None, str | None]:
    value = uri.strip()
    parts = value.split(":")
    if len(parts) == 5 and parts[:2] == ["applemusic", "catalog"] and parts[3] == "song":
        return parts[2].lower() or None, parts[4] or None
    if len(parts) == 3 and parts[:2] == ["applemusic", "song"]:
        return None, parts[2] or None
    if value.isdigit():
        return None, value
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None, None
    if parsed.hostname not in {"music.apple.com", "geo.music.apple.com"}:
        return None, None
    query_id = urllib.parse.parse_qs(parsed.query).get("i", [None])[0]
    path_parts = [part for part in parsed.path.split("/") if part]
    storefront = path_parts[0].lower() if path_parts else None
    if isinstance(query_id, str) and query_id:
        return storefront, query_id
    tail = path_parts[-1] if path_parts else ""
    return (storefront, tail) if tail.isdigit() else (None, None)


def _playlist_ref(resource: dict[str, Any]) -> PlaylistRef:
    attrs = _attributes(resource)
    relationships = resource.get("relationships")
    tracks = relationships.get("tracks") if isinstance(relationships, dict) else None
    meta = tracks.get("meta") if isinstance(tracks, dict) else None
    total = meta.get("total") if isinstance(meta, dict) else None
    return PlaylistRef(
        id=str(resource.get("id") or ""),
        name=str(attrs.get("name") or ""),
        track_count=total if isinstance(total, int) else None,
        owner_id=_ACCOUNT_ID,
        owner_name="Apple Music account",
        is_owned=True,
        is_followed=False,
        collaborative=False,
        created_at=attrs.get("dateAdded"),
    )


def _catalog_id(resource: dict[str, Any]) -> str | None:
    attrs = _attributes(resource)
    play_params = attrs.get("playParams")
    if not isinstance(play_params, dict):
        return None
    value = play_params.get("catalogId")
    return value if isinstance(value, str) and value else None


def _track_from_library_song(
    resource: dict[str, Any],
    *,
    storefront: str,
    catalog_by_id: dict[str, dict[str, Any]],
    position: int,
) -> Track:
    attrs = _attributes(resource)
    library_id = str(resource.get("id") or "")
    catalog_id = _catalog_id(resource)
    catalog_attrs = _attributes(catalog_by_id.get(catalog_id or ""))
    release_date, release_year = _release_date(
        catalog_attrs.get("releaseDate") or attrs.get("releaseDate")
    )
    composer = catalog_attrs.get("composerName")
    if not isinstance(composer, str):
        composer = None
    resource_type = resource.get("type")
    media_type = (
        MediaType.TRACK if resource_type == "library-songs" else MediaType.VIDEO
    )
    provider_uri = (
        _catalog_uri(storefront, catalog_id)
        if catalog_id
        else _library_uri(library_id)
    )
    genre_names = catalog_attrs.get("genreNames") or attrs.get("genreNames")
    genres = [value for value in genre_names if isinstance(value, str)] if isinstance(
        genre_names, list
    ) else []
    track = Track(
        id=catalog_id or library_id or None,
        title=str(attrs.get("name") or catalog_attrs.get("name") or library_id),
        artist=str(attrs.get("artistName") or catalog_attrs.get("artistName") or "Unknown"),
        album=(
            attrs.get("albumName")
            if isinstance(attrs.get("albumName"), str)
            else catalog_attrs.get("albumName")
            if isinstance(catalog_attrs.get("albumName"), str)
            else None
        ),
        duration_s=_duration_s(
            attrs.get("durationInMillis") or catalog_attrs.get("durationInMillis")
        ),
        release_date=release_date,
        release_year=release_year,
        genre=genres[0] if genres else None,
        track_number=(
            attrs.get("trackNumber")
            if isinstance(attrs.get("trackNumber"), int)
            else catalog_attrs.get("trackNumber")
            if isinstance(catalog_attrs.get("trackNumber"), int)
            else None
        ),
        disc_number=(
            attrs.get("discNumber")
            if isinstance(attrs.get("discNumber"), int)
            else catalog_attrs.get("discNumber")
            if isinstance(catalog_attrs.get("discNumber"), int)
            else None
        ),
        explicit=_first_explicit(catalog_attrs, attrs),
        composer=composer,
        credits=[{"role": "composer", "name": composer}] if composer else [],
        isrc=(
            catalog_attrs.get("isrc")
            if isinstance(catalog_attrs.get("isrc"), str)
            else None
        ),
        artwork_uri=_artwork_uri(attrs) or _artwork_uri(catalog_attrs),
        provider_uris={"applemusic": provider_uri},
        metadata={
            "applemusic_library_id": library_id,
            "applemusic_catalog_id": catalog_id,
            "applemusic_url": catalog_attrs.get("url"),
            "applemusic_genres": genres,
        },
        position=position,
        media_type=media_type,
        source_item_id=library_id or None,
    )
    if media_type is not MediaType.TRACK:
        track.unsupported_reason = f"unsupported Apple Music item type: {resource_type}"
    return track


def _candidate(resource: dict[str, Any], storefront: str) -> TrackCandidate:
    attrs = _attributes(resource)
    song_id = str(resource.get("id") or "")
    return TrackCandidate(
        provider_track_id=song_id,
        uri=_catalog_uri(storefront, song_id),
        title=str(attrs.get("name") or song_id),
        artist=str(attrs.get("artistName") or "Unknown"),
        album=attrs.get("albumName") if isinstance(attrs.get("albumName"), str) else None,
        duration_s=_duration_s(attrs.get("durationInMillis")),
        isrc=attrs.get("isrc") if isinstance(attrs.get("isrc"), str) else None,
        explicit=_is_explicit(attrs),
        market=storefront,
    )


class AppleMusicAuth(AuthStrategy):
    kind = AuthKind.DEVELOPER_USER_TOKEN

    def __init__(
        self,
        *,
        token_provider: AppleDeveloperTokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        developer_token = self._token_provider.get()
        return AuthChallenge(
            shape=ChallengeShape.FORM,
            instructions=(
                "Authorize with Apple Music in the browser. Apple uses a MusicKit "
                "developer token plus a Music User Token instead of OAuth client credentials."
            ),
            form_schema={
                "music_user_token": {
                    "type": "string",
                    "format": "musickit",
                    "developer_token": developer_token,
                    "script_url": _MUSICKIT_JS_URL,
                }
            },
        )

    async def complete(
        self, *, user_id: str, callback: dict[str, Any]
    ) -> ProviderCredential:
        music_user_token = callback.get("music_user_token")
        if not isinstance(music_user_token, str) or not music_user_token.strip():
            raise ProviderError("Apple Music auth requires a Music User Token")
        storefront = await self._fetch_storefront(music_user_token.strip())
        return ProviderCredential(
            account_id=_ACCOUNT_ID,
            provider="applemusic",
            auth_kind=self.kind,
            access_token=music_user_token.strip(),
            extra={
                "display_name": f"Apple Music ({storefront.upper()})",
                "storefront": storefront,
            },
        )

    async def _fetch_storefront(self, music_user_token: str) -> str:
        async with httpx.AsyncClient(
            base_url=_API_ORIGIN,
            transport=self._transport,
            headers=_headers(self._token_provider.get(), music_user_token),
            timeout=30.0,
        ) as client:
            resp = _raise_for_status(
                await _apple_request(client, "GET", "/v1/me/storefront")
            )
        resources = _resources(resp.json())
        if not resources or not resources[0].get("id"):
            raise ProviderError("Apple Music storefront response did not include a storefront")
        return str(resources[0]["id"]).lower()

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        storefront = await self._fetch_storefront(_music_user_token(cred))
        return cred.model_copy(
            update={
                "extra": {
                    **cred.extra,
                    "display_name": f"Apple Music ({storefront.upper()})",
                    "storefront": storefront,
                }
            }
        )

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


class AppleMusicAdapter:
    info = ProviderInfo(
        name="applemusic",
        display_name="Apple Music",
        auth_kind=AuthKind.DEVELOPER_USER_TOKEN,
        capabilities=CapabilityDescriptor(
            capabilities={
                Capability.READ_PLAYLISTS,
                Capability.READ_TRACKS,
                Capability.READ_LIBRARY,
                Capability.CREATE_PLAYLIST,
                Capability.ADD_TRACKS,
                Capability.SET_DESCRIPTION,
            },
            has_isrc=True,
            search_modes=[SearchMode.ISRC, SearchMode.TEXT],
            official=True,
            stability=Stability.BETA,
            max_add_batch=_MAX_ADD_BATCH,
            supports_duplicates=True,
            ordering=OrderingGuarantee.BEST_EFFORT,
            warning=(
                "Requires an Apple Developer Program MusicKit key and an active "
                "Apple Music subscription. Library writes can take time to appear."
            ),
        ),
    )

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        developer_token: str | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._token_provider = AppleDeveloperTokenProvider(token=developer_token)
        self._sleep = sleep
        self._recently_created: dict[str, float] = {}
        self.auth = AppleMusicAuth(
            token_provider=self._token_provider,
            transport=transport,
        )

    def _client(self, cred: ProviderCredential) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API_ORIGIN,
            transport=self._transport,
            headers=_headers(self._token_provider.get(), _music_user_token(cred)),
            timeout=30.0,
        )

    async def _storefront_for_cred(
        self, client: httpx.AsyncClient, cred: ProviderCredential
    ) -> str:
        cached = _storefront(cred)
        if cached:
            return cached
        return await self._fetch_storefront(client)

    async def _fetch_storefront(self, client: httpx.AsyncClient) -> str:
        resp = _raise_for_status(
            await _apple_request(client, "GET", "/v1/me/storefront")
        )
        resources = _resources(resp.json())
        if not resources or not resources[0].get("id"):
            raise ProviderError("Apple Music storefront response did not include a storefront")
        return str(resources[0]["id"]).lower()

    async def iter_playlists(
        self, cred: ProviderCredential
    ) -> AsyncIterator[PlaylistRef]:
        async with self._client(cred) as client:
            url = "/v1/me/library/playlists"
            params: dict[str, Any] | None = {"limit": _LIST_PAGE}
            while url:
                resp = _raise_for_status(
                    await _apple_request(client, "GET", url, params=params)
                )
                params = None
                payload = resp.json()
                for resource in _resources(payload):
                    yield _playlist_ref(resource)
                url = _next_url(payload) or ""

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        playlist = await self.read_playlist(cred, ref)
        for track in playlist.tracks:
            yield track

    async def read_playlist(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> Playlist:
        async with self._client(cred) as client:
            storefront = await self._storefront_for_cred(client, cred)
            meta_resp = _raise_for_status(
                await _apple_request(
                    client,
                    "GET",
                    f"/v1/me/library/playlists/{ref.id}",
                )
            )
            meta_resources = _resources(meta_resp.json())
            if not meta_resources:
                raise ProviderError(
                    "Apple Music playlist response did not include playlist data"
                )
            resource = meta_resources[0]
            attrs = _attributes(resource)
            items = await self._library_playlist_items(client, ref.id)
            catalog_ids = [catalog_id for item in items if (catalog_id := _catalog_id(item))]
            catalog_by_id = await self._catalog_songs(client, storefront, catalog_ids)
        tracks = [
            _track_from_library_song(
                item,
                storefront=storefront,
                catalog_by_id=catalog_by_id,
                position=position,
            )
            for position, item in enumerate(items)
        ]
        return Playlist(
            id=str(resource.get("id") or ref.id),
            name=str(attrs.get("name") or ref.name),
            description=_description(attrs),
            photo=_artwork_uri(attrs),
            tracks=tracks,
            owner_id=_ACCOUNT_ID,
            owner_name="Apple Music account",
            is_owned=True,
            is_followed=False,
            collaborative=False,
            created_at=attrs.get("dateAdded"),
        )

    async def _library_playlist_items(
        self, client: httpx.AsyncClient, playlist_id: str
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url = f"/v1/me/library/playlists/{playlist_id}/tracks"
        params: dict[str, Any] | None = {"limit": _LIST_PAGE}
        while url:
            resp = _raise_for_status(
                await _apple_request(client, "GET", url, params=params)
            )
            params = None
            payload = resp.json()
            items.extend(_resources(payload))
            url = _next_url(payload) or ""
        return items

    async def _catalog_songs(
        self,
        client: httpx.AsyncClient,
        storefront: str,
        ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(song_id for song_id in ids if song_id))
        for start in range(0, len(unique_ids), _CATALOG_BATCH):
            chunk = unique_ids[start : start + _CATALOG_BATCH]
            resp = _raise_for_status(
                await _apple_request(
                    client,
                    "GET",
                    f"/v1/catalog/{storefront}/songs",
                    params={"ids": ",".join(chunk)},
                )
            )
            for resource in _resources(resp.json()):
                song_id = resource.get("id")
                if isinstance(song_id, str):
                    out[song_id] = resource
        return out

    async def test_connection(self, cred: ProviderCredential) -> None:
        async with self._client(cred) as client:
            await self._fetch_storefront(client)

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        limit = max(1, min(limit, 25))
        async with self._client(cred) as client:
            storefront = await self._storefront_for_cred(client, cred)
            if track.isrc:
                results = await self._search_by_isrc(
                    client,
                    storefront,
                    track.isrc,
                    limit=limit,
                )
                if results:
                    return results
            query = f"{track.title} {track.artist}".strip()
            if track.album:
                query = f"{query} {track.album}".strip()
            resp = _raise_for_status(
                await _apple_request(
                    client,
                    "GET",
                    f"/v1/catalog/{storefront}/search",
                    params={"types": "songs", "term": query, "limit": limit},
                )
            )
        results = resp.json().get("results")
        songs = results.get("songs") if isinstance(results, dict) else None
        data = songs.get("data") if isinstance(songs, dict) else None
        resources = [item for item in data if isinstance(item, dict)] if isinstance(
            data, list
        ) else []
        return [_candidate(resource, storefront) for resource in resources[:limit]]

    async def _search_by_isrc(
        self,
        client: httpx.AsyncClient,
        storefront: str,
        isrc: str,
        *,
        limit: int,
    ) -> list[TrackCandidate]:
        resp = _raise_for_status(
            await _apple_request(
                client,
                "GET",
                f"/v1/catalog/{storefront}/songs",
                params={"filter[isrc]": isrc, "limit": limit},
            )
        )
        payload = resp.json()
        resources = _resources(payload)
        known = {
            str(resource.get("id")): resource
            for resource in resources
            if resource.get("id")
        }
        filters = payload.get("meta")
        filters = filters.get("filters") if isinstance(filters, dict) else None
        isrc_map = filters.get("isrc") if isinstance(filters, dict) else None
        identifiers = isrc_map.get(isrc) if isinstance(isrc_map, dict) else None
        ordered_ids = [
            str(item["id"])
            for item in identifiers
            if isinstance(item, dict) and item.get("id")
        ] if isinstance(identifiers, list) else list(known)
        missing = [song_id for song_id in ordered_ids[:limit] if song_id not in known]
        if missing:
            known.update(await self._catalog_songs(client, storefront, missing))
        ordered = [known[song_id] for song_id in ordered_ids[:limit] if song_id in known]
        if not ordered:
            ordered = resources[:limit]
        return [_candidate(resource, storefront) for resource in ordered]

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        uri_storefront, song_id = _catalog_song_from_uri(uri)
        if not song_id:
            return False
        async with self._client(cred) as client:
            storefront = await self._storefront_for_cred(client, cred)
            if uri_storefront and uri_storefront != storefront:
                return False
            resp = await _apple_request(
                client,
                "GET",
                f"/v1/catalog/{storefront}/songs/{song_id}",
            )
        if resp.status_code == 404:
            return False
        _raise_for_status(resp)
        return True

    async def create_playlist(
        self, cred: ProviderCredential, spec: CreatePlaylistSpec
    ) -> str:
        attributes: dict[str, Any] = {"name": spec.name}
        if spec.description:
            attributes["description"] = spec.description
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _apple_request(
                    client,
                    "POST",
                    "/v1/me/library/playlists",
                    json={"attributes": attributes},
                )
            )
        resources = _resources(resp.json())
        if not resources or not resources[0].get("id"):
            raise ProviderError(
                "Apple Music create playlist response did not include a playlist id"
            )
        playlist_id = str(resources[0]["id"])
        self._recently_created[playlist_id] = time.monotonic()
        return playlist_id

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        results: list[AddItemResult | None] = [None] * len(uris)
        async with self._client(cred) as client:
            storefront = await self._storefront_for_cred(client, cred)
            for start in range(0, len(uris), self.info.capabilities.max_add_batch):
                chunk = list(uris[start : start + self.info.capabilities.max_add_batch])
                payload: list[dict[str, str]] = []
                valid_positions: list[tuple[int, str]] = []
                for offset, uri in enumerate(chunk):
                    uri_storefront, song_id = _catalog_song_from_uri(uri)
                    position = start + offset
                    if not song_id:
                        results[position] = AddItemResult(
                            uri=uri,
                            ok=False,
                            error="invalid Apple Music catalog song URI",
                        )
                        continue
                    if uri_storefront and uri_storefront != storefront:
                        results[position] = AddItemResult(
                            uri=uri,
                            ok=False,
                            error=(
                                f"Apple Music song belongs to storefront {uri_storefront}, "
                                f"not {storefront}"
                            ),
                        )
                        continue
                    payload.append({"id": song_id, "type": "songs"})
                    valid_positions.append((position, uri))
                if not payload:
                    continue
                await self._post_tracks(client, playlist_id, payload)
                for position, uri in valid_positions:
                    results[position] = AddItemResult(
                        uri=uri,
                        ok=True,
                        position=position,
                    )
        return [
            result
            if result is not None
            else AddItemResult(uri=uris[position], ok=False, error="track was not processed")
            for position, result in enumerate(results)
        ]

    async def unfollow_playlist(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> PlaylistMutationResult:
        raise Unsupported("Apple Music does not expose playlist unfollow through MusicKit")

    async def delete_playlist(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> PlaylistMutationResult:
        raise Unsupported("Apple Music does not expose library playlist deletion through MusicKit")

    async def remove_tracks(
        self,
        cred: ProviderCredential,
        ref: PlaylistRef,
        items: Sequence[TrackRemoval],
    ) -> RemoveTracksResult:
        raise Unsupported(
            "Apple Music does not expose removal of songs from library playlists through MusicKit"
        )

    async def _post_tracks(
        self,
        client: httpx.AsyncClient,
        playlist_id: str,
        payload: list[dict[str, str]],
    ) -> None:
        retry_delays = iter(_NEW_PLAYLIST_RETRY_DELAYS_S)
        while True:
            resp = await _apple_request(
                client,
                "POST",
                f"/v1/me/library/playlists/{playlist_id}/tracks",
                json={"data": payload},
            )
            if resp.status_code != 404 or not self._is_recently_created(playlist_id):
                _raise_for_status(resp)
                self._recently_created.pop(playlist_id, None)
                return
            delay = next(retry_delays, None)
            if delay is None:
                _raise_for_status(resp)
            await self._sleep(delay)

    def _is_recently_created(self, playlist_id: str) -> bool:
        created_at = self._recently_created.get(playlist_id)
        if created_at is None:
            return False
        if time.monotonic() - created_at <= _NEW_PLAYLIST_WINDOW_S:
            return True
        self._recently_created.pop(playlist_id, None)
        return False


adapter = register(AppleMusicAdapter())
