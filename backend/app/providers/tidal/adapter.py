"""Tidal adapter — official JSON:API read/search/write over OAuth PKCE."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

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
    ProviderCredential,
    ProviderError,
    ProviderInfo,
    RateLimited,
    RefreshTokenExpired,
    TrackCandidate,
)
from app.core.capabilities import Capability, CapabilityDescriptor, SearchMode, Stability
from app.core.models import MediaType, Playlist, PlaylistRef, Track
from app.core.registry import register
from app.settings import get_settings

_API_BASE = "https://openapi.tidal.com/v2"
_AUTH_URL = "https://login.tidal.com/authorize"
_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
_JSON_API = "application/vnd.api+json"
_STATE_TTL_S = 600
_MAX_ADD_BATCH = 50
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_MAX_AUTO_WAIT_S = 30
_SCOPES = [
    "playlists.read",
    "playlists.write",
    "search.read",
    "user.read",
]
_TRACK_INCLUDES = ("albums", "artists", "credits")
_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?"
    r"(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?$"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingState:
    user_id: str
    code_verifier: str
    created_at: float


@dataclass(frozen=True)
class _CatalogToken:
    access_token: str
    expires_at: float | None


_PENDING_STATES: dict[str, _PendingState] = {}


def _raise_for_status(resp: httpx.Response) -> httpx.Response:
    if resp.is_success:
        return resp
    if resp.status_code == 401:
        raise AuthExpired("tidal authorization expired; reconnect Tidal")
    if resp.status_code == 403:
        raise AccessDenied("tidal request forbidden")
    if resp.status_code == 404:
        raise NotFound(str(resp.request.url))
    if resp.status_code == 429:
        retry_after_s = _retry_after_seconds(resp)
        message = "tidal rate limited"
        if retry_after_s is not None:
            message = f"{message}; retry after {retry_after_s:g} seconds"
        raise RateLimited(
            retry_after_s=retry_after_s,
            message=message,
            status_code=resp.status_code,
        )
    raise ProviderError(f"tidal HTTP {resp.status_code}: {_tidal_error_message(resp)}")


async def _tidal_request(
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
            logger.warning(
                "tidal rate limited retry_after_s=%s retry=%s/%s path=%s",
                retry_after_s,
                retries,
                _RATE_LIMIT_MAX_RETRIES,
                resp.request.url.path,
            )
            return resp
        retries += 1
        logger.warning(
            "tidal rate limited retry_after_s=%s retry=%s/%s path=%s",
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


def _tidal_error_message(resp: httpx.Response) -> str:
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
        for key in ("error_description", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return resp.reason_phrase


def _tidal_error_code(resp: httpx.Response) -> str | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = errors[0].get("code")
            return code if isinstance(code, str) and code else None
    return None


def _token_auth(settings) -> tuple[str, str] | None:
    if settings.tidal_client_secret:
        return (settings.tidal_client_id, settings.tidal_client_secret)
    return None


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
        raise ProviderError("tidal callback is missing state")
    pending = _PENDING_STATES.pop(state, None)
    if pending is None:
        raise ProviderError("tidal callback state is invalid or expired")
    if pending.user_id != user_id:
        raise ProviderError("tidal callback state does not match the current user")
    return pending


def _expires_at(expires_in: int | None) -> float | None:
    if not expires_in:
        return None
    return time.time() + max(0, expires_in - 30)


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": _JSON_API,
        "Content-Type": _JSON_API,
        "Authorization": f"Bearer {access_token}",
    }


def _country(cred: ProviderCredential) -> str | None:
    value = cred.extra.get("country")
    return value if isinstance(value, str) and value else None


def _params(
    cred: ProviderCredential, pairs: Sequence[tuple[str, str]] | None = None
) -> list[tuple[str, str]]:
    params = list(pairs or [])
    country = _country(cred)
    if country:
        params.append(("countryCode", country))
    return params


def _include_params(*values: str) -> list[tuple[str, str]]:
    return [("include", value) for value in values]


def _attributes(resource: dict[str, Any] | None) -> dict[str, Any]:
    attrs = (resource or {}).get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _relationships(resource: dict[str, Any] | None) -> dict[str, Any]:
    rels = (resource or {}).get("relationships")
    return rels if isinstance(rels, dict) else {}


def _relationship_data(resource: dict[str, Any], name: str) -> Any:
    relationship = _relationships(resource).get(name)
    if not isinstance(relationship, dict):
        return None
    return relationship.get("data")


def _included_index(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    included = payload.get("included")
    if not isinstance(included, list):
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for resource in included:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("type")
        resource_id = resource.get("id")
        if isinstance(resource_type, str) and isinstance(resource_id, str):
            out[(resource_type, resource_id)] = resource
    return out


def _resource_id(resource: dict[str, Any] | None) -> str | None:
    value = (resource or {}).get("id")
    return value if isinstance(value, str) and value else None


def _first_related(
    resource: dict[str, Any], included: dict[tuple[str, str], dict[str, Any]], name: str
) -> dict[str, Any] | None:
    data = _relationship_data(resource, name)
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    resource_type = first.get("type")
    resource_id = first.get("id")
    if not isinstance(resource_type, str) or not isinstance(resource_id, str):
        return None
    return included.get((resource_type, resource_id))


def _related_resources(
    resource: dict[str, Any], included: dict[tuple[str, str], dict[str, Any]], name: str
) -> list[dict[str, Any]]:
    data = _relationship_data(resource, name)
    if not isinstance(data, list):
        return []
    resources = []
    for item in data:
        if not isinstance(item, dict):
            continue
        resource_type = item.get("type")
        resource_id = item.get("id")
        if not isinstance(resource_type, str) or not isinstance(resource_id, str):
            continue
        resource = included.get((resource_type, resource_id))
        if resource is not None:
            resources.append(resource)
    return resources


def _artist_names(
    resource: dict[str, Any], included: dict[tuple[str, str], dict[str, Any]]
) -> str:
    names = [
        attrs.get("name")
        for attrs in (
            _attributes(artist) for artist in _related_resources(resource, included, "artists")
        )
        if isinstance(attrs.get("name"), str) and attrs.get("name")
    ]
    return ", ".join(names)


def _credits(
    resource: dict[str, Any], included: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, str]]:
    credits = []
    for credit in _related_resources(resource, included, "credits"):
        attrs = _attributes(credit)
        role = attrs.get("role")
        name = attrs.get("name")
        if isinstance(role, str) and isinstance(name, str) and role and name:
            credits.append({"role": role, "name": name})
    return credits


def _duration_s(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    match = _DURATION.match(value)
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return round(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _release_date(raw: Any) -> tuple[date | None, int | None]:
    if not isinstance(raw, str) or not raw:
        return None, None
    year = int(raw[:4]) if len(raw) >= 4 and raw[:4].isdigit() else None
    try:
        return date.fromisoformat(raw), year
    except ValueError:
        return None, year


def _next_url(payload: dict[str, Any]) -> str | None:
    links = payload.get("links")
    if not isinstance(links, dict):
        return None
    next_url = links.get("next")
    return next_url if isinstance(next_url, str) and next_url else None


def _track_id(uri: str) -> str | None:
    uri = uri.strip()
    if uri.startswith("tidal:track:"):
        return uri.rsplit(":", 1)[-1] or None
    if "/track/" in uri:
        tail = uri.split("/track/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0] or None
    if "/browse/track/" in uri:
        tail = uri.split("/browse/track/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0] or None
    return uri or None


def _playlist_ref(resource: dict[str, Any]) -> PlaylistRef:
    attrs = _attributes(resource)
    owners = _relationship_data(resource, "owners")
    owner_id = None
    if isinstance(owners, list) and owners and isinstance(owners[0], dict):
        raw_owner = owners[0].get("id")
        owner_id = raw_owner if isinstance(raw_owner, str) else None
    return PlaylistRef(
        id=str(resource.get("id") or ""),
        name=str(attrs.get("name") or ""),
        track_count=attrs.get("numberOfItems")
        if isinstance(attrs.get("numberOfItems"), int)
        else None,
        owner_id=owner_id,
        snapshot_id=attrs.get("lastModifiedAt")
        if isinstance(attrs.get("lastModifiedAt"), str)
        else None,
    )


def _track_from_resource(
    resource: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
    *,
    position: int | None = None,
    item_meta: dict[str, Any] | None = None,
) -> Track:
    attrs = _attributes(resource)
    track_id = str(resource.get("id") or "")
    album = _first_related(resource, included, "albums")
    album_attrs = _attributes(album)
    release_date, release_year = _release_date(album_attrs.get("releaseDate"))
    title = str(attrs.get("title") or track_id)
    version = attrs.get("version")
    metadata = {
        key: value
        for key, value in {
            "tidal_media_tags": attrs.get("mediaTags"),
            "tidal_popularity": attrs.get("popularity"),
            "tidal_version": version,
        }.items()
        if value is not None
    }
    return Track(
        id=track_id,
        title=title,
        artist=_artist_names(resource, included) or "Unknown",
        album=album_attrs.get("title") if isinstance(album_attrs.get("title"), str) else None,
        duration_s=_duration_s(attrs.get("duration")),
        release_date=release_date,
        release_year=release_year,
        explicit=attrs.get("explicit") if isinstance(attrs.get("explicit"), bool) else None,
        credits=_credits(resource, included),
        isrc=attrs.get("isrc") if isinstance(attrs.get("isrc"), str) else None,
        provider_uris={"tidal": f"tidal:track:{track_id}"} if track_id else {},
        metadata=metadata,
        position=position,
        source_item_id=track_id or None,
        added_at=(item_meta or {}).get("addedAt"),
    )


def _unsupported_item(
    item: dict[str, Any], *, position: int, included: dict[tuple[str, str], dict[str, Any]]
) -> Track:
    item_id = str(item.get("id") or "")
    item_type = str(item.get("type") or "unknown")
    resource = included.get((item_type, item_id))
    attrs = _attributes(resource)
    title = (
        attrs.get("title")
        if isinstance(attrs.get("title"), str)
        else f"Tidal {item_type} {item_id}"
    )
    track = Track(
        id=item_id or None,
        title=title,
        artist="",
        media_type=MediaType.VIDEO if item_type == "videos" else MediaType.UNKNOWN,
        provider_uris={"tidal": f"tidal:{item_type.removesuffix('s')}:{item_id}"}
        if item_id
        else {},
        position=position,
        source_item_id=item_id or None,
        unsupported_reason=f"unsupported item type: {item_type}",
    )
    return track


def _candidate(
    resource: dict[str, Any], included: dict[tuple[str, str], dict[str, Any]]
) -> TrackCandidate:
    track = _track_from_resource(resource, included)
    return TrackCandidate(
        provider_track_id=track.id or "",
        uri=track.provider_uris.get("tidal") or f"tidal:track:{track.id}",
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_s=track.duration_s,
        isrc=track.isrc,
        explicit=track.explicit,
    )


def _resources_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _resources_from_relationship_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    included = _included_index(payload)
    if not isinstance(data, list):
        return []
    resources = []
    for item in data:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        item_id = item.get("id")
        if isinstance(item_type, str) and isinstance(item_id, str):
            resource = included.get((item_type, item_id))
            if resource is not None:
                resources.append(resource)
    return resources


class TidalAuth(AuthStrategy):
    kind = AuthKind.OAUTH_PKCE

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        settings = get_settings()
        if not settings.tidal_client_id:
            raise ProviderError("OPE_TIDAL_CLIENT_ID is required before connecting Tidal")
        state = uuid.uuid4().hex
        code_verifier = secrets.token_urlsafe(64)
        _store_state(
            state,
            _PendingState(user_id=user_id, code_verifier=code_verifier, created_at=time.time()),
        )
        params = {
            "client_id": settings.tidal_client_id,
            "response_type": "code",
            "redirect_uri": settings.tidal_redirect_uri,
            "scope": " ".join(_SCOPES),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": _code_challenge(code_verifier),
        }
        url = _AUTH_URL + "?" + urllib.parse.urlencode(params)
        return AuthChallenge(shape=ChallengeShape.REDIRECT, redirect_url=url, state=state)

    async def complete(self, *, user_id: str, callback: dict[str, Any]) -> ProviderCredential:
        if callback.get("error"):
            raise ProviderError(f"tidal authorization failed: {callback['error']}")
        code = callback.get("code")
        if not isinstance(code, str) or not code:
            raise ProviderError("tidal callback is missing code")
        pending = _consume_state(callback.get("state"), user_id)
        settings = get_settings()
        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            resp = await _tidal_request(
                client,
                "POST",
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.tidal_redirect_uri,
                    "client_id": settings.tidal_client_id,
                    "code_verifier": pending.code_verifier,
                },
                auth=_token_auth(settings),
            )
        if not resp.is_success:
            _raise_for_status(resp)
            raise ProviderError(f"tidal token exchange failed with HTTP {resp.status_code}")
        token = resp.json()
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("tidal token response did not include an access token")
        profile = await self._profile(access_token)
        account_id = profile["account_id"]
        return ProviderCredential(
            account_id=account_id,
            provider="tidal",
            auth_kind=self.kind,
            access_token=access_token,
            refresh_token=token.get("refresh_token"),
            expires_at=_expires_at(token.get("expires_in")),
            scopes=(token.get("scope") or " ".join(_SCOPES)).split(),
            extra={
                "display_name": profile["display_name"],
                "country": profile.get("country"),
                "email": profile.get("email"),
                "username": profile.get("username"),
            },
        )

    async def _profile(self, access_token: str) -> dict[str, str | None]:
        async with httpx.AsyncClient(
            base_url=_API_BASE,
            transport=self._transport,
            headers=_headers(access_token),
            timeout=30.0,
        ) as client:
            resp = _raise_for_status(await _tidal_request(client, "GET", "/users/me"))
        data = resp.json().get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise ProviderError("tidal user response did not include a user id")
        attrs = _attributes(data)
        username = attrs.get("username") if isinstance(attrs.get("username"), str) else None
        email = attrs.get("email") if isinstance(attrs.get("email"), str) else None
        display_name = username or email or str(data["id"])
        country = attrs.get("country") if isinstance(attrs.get("country"), str) else None
        return {
            "account_id": str(data["id"]),
            "display_name": display_name,
            "country": country,
            "email": email,
            "username": username,
        }

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        if not cred.refresh_token:
            raise RefreshTokenExpired("tidal refresh token is missing; reconnect Tidal")
        settings = get_settings()
        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            resp = await _tidal_request(
                client,
                "POST",
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": cred.refresh_token,
                    "client_id": settings.tidal_client_id,
                },
                auth=_token_auth(settings),
            )
        if not resp.is_success:
            if resp.status_code == 400 and _tidal_error_code(resp) == "invalid_grant":
                raise RefreshTokenExpired("tidal refresh token expired; reconnect Tidal")
            _raise_for_status(resp)
            raise AuthExpired("tidal authorization expired; reconnect Tidal")
        token = resp.json()
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthExpired("tidal refresh response did not include an access token")
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


class TidalAdapter:
    info = ProviderInfo(
        name="tidal",
        display_name="Tidal",
        auth_kind=AuthKind.OAUTH_PKCE,
        capabilities=CapabilityDescriptor(
            capabilities={
                Capability.READ_PLAYLISTS,
                Capability.READ_TRACKS,
                Capability.CREATE_PLAYLIST,
                Capability.ADD_TRACKS,
                Capability.SET_DESCRIPTION,
            },
            has_isrc=True,
            search_modes=[SearchMode.ISRC, SearchMode.TEXT],
            official=True,
            stability=Stability.STABLE,
            max_add_batch=_MAX_ADD_BATCH,
            supports_duplicates=True,
            warning=(
                "Requires a TIDAL developer app with the playlists.read, playlists.write, "
                "search.read, and user.read scopes."
            ),
        ),
    )
    auth = TidalAuth()

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._catalog_token: _CatalogToken | None = None

    def _client(self, cred: ProviderCredential) -> httpx.AsyncClient:
        if not cred.access_token:
            raise AuthExpired("missing tidal access token")
        return httpx.AsyncClient(
            base_url=_API_BASE,
            transport=self._transport,
            headers=_headers(cred.access_token),
            timeout=30.0,
        )

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        async with self._client(cred) as client:
            params = _params(cred, [("filter[owners.id]", "me"), ("sort", "name")])
            resp = _raise_for_status(
                await _tidal_request(client, "GET", "/playlists", params=params)
            )
            while True:
                payload = resp.json()
                for resource in _resources_from_payload(payload):
                    yield _playlist_ref(resource)
                next_url = _next_url(payload)
                if not next_url:
                    break
                resp = _raise_for_status(await _tidal_request(client, "GET", next_url))

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        playlist = await self.read_playlist(cred, ref)
        for track in playlist.tracks:
            yield track

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        async with self._client(cred) as client:
            meta_resp = _raise_for_status(
                await _tidal_request(
                    client,
                    "GET",
                    f"/playlists/{ref.id}",
                    params=_params(cred),
                )
            )
            meta_payload = meta_resp.json()
            playlist_resource = meta_payload.get("data")
            if not isinstance(playlist_resource, dict):
                raise ProviderError("tidal playlist response did not include playlist data")
            attrs = _attributes(playlist_resource)
            tracks = await self._read_playlist_tracks(client, cred, ref.id)
        return Playlist(
            id=str(playlist_resource.get("id") or ref.id),
            name=str(attrs.get("name") or ref.name),
            description=attrs.get("description")
            if isinstance(attrs.get("description"), str)
            else None,
            owner_id=_playlist_ref(playlist_resource).owner_id,
            snapshot_id=attrs.get("lastModifiedAt")
            if isinstance(attrs.get("lastModifiedAt"), str)
            else ref.snapshot_id,
            tracks=tracks,
        )

    async def _read_playlist_tracks(
        self, client: httpx.AsyncClient, cred: ProviderCredential, playlist_id: str
    ) -> list[Track]:
        params = _params(
            cred,
            [
                ("sort", "itemIndex"),
                *_include_params("items"),
            ],
        )
        resp = _raise_for_status(
            await _tidal_request(
                client,
                "GET",
                f"/playlists/{playlist_id}/relationships/items",
                params=params,
            )
        )
        tracks: list[Track] = []
        missing_track_ids: list[tuple[int, str, dict[str, Any]]] = []
        while True:
            payload = resp.json()
            included = _included_index(payload)
            data = payload.get("data")
            if not isinstance(data, list):
                return tracks
            for item in data:
                if not isinstance(item, dict):
                    continue
                position = len(tracks)
                item_id = item.get("id")
                item_type = item.get("type")
                item_meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                if item_type != "tracks" or not isinstance(item_id, str):
                    tracks.append(_unsupported_item(item, position=position, included=included))
                    continue
                resource = included.get(("tracks", item_id))
                if resource is None:
                    missing_track_ids.append((position, item_id, item_meta))
                    tracks.append(
                        Track(
                            id=item_id,
                            title=item_id,
                            artist="Unknown",
                            provider_uris={"tidal": f"tidal:track:{item_id}"},
                            position=position,
                            source_item_id=item_id,
                            added_at=item_meta.get("addedAt"),
                        )
                    )
                    continue
                tracks.append(
                    _track_from_resource(
                        resource,
                        included,
                        position=position,
                        item_meta=item_meta,
                    )
                )
            next_url = _next_url(payload)
            if not next_url:
                break
            resp = _raise_for_status(await _tidal_request(client, "GET", next_url))
        if missing_track_ids:
            fetched = await self._fetch_tracks(
                client, cred, [item_id for _, item_id, _ in missing_track_ids]
            )
            for position, item_id, item_meta in missing_track_ids:
                resource = fetched.get(item_id)
                if resource is not None:
                    tracks[position] = _track_from_resource(
                        resource,
                        fetched.included,
                        position=position,
                        item_meta=item_meta,
                    )
        return tracks

    async def test_connection(self, cred: ProviderCredential) -> None:
        async with self._client(cred) as client:
            _raise_for_status(await _tidal_request(client, "GET", "/users/me"))

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        async with self._client(cred) as client:
            if track.isrc:
                isrc_results = await self._search_tracks_by_isrc(cred, track.isrc, limit=limit)
                if isrc_results:
                    return isrc_results
            return await self._search_tracks_by_text(client, cred, track, limit=limit)

    async def _search_tracks_by_isrc(
        self, cred: ProviderCredential, isrc: str, *, limit: int
    ) -> list[TrackCandidate]:
        catalog_token = await self._catalog_access_token()
        if catalog_token is None:
            return []
        params = _params(
            cred,
            [
                ("filter[isrc]", isrc),
                *_include_params(*_TRACK_INCLUDES),
            ],
        )
        async with httpx.AsyncClient(
            base_url=_API_BASE,
            transport=self._transport,
            headers=_headers(catalog_token),
            timeout=30.0,
        ) as client:
            resp = _raise_for_status(
                await _tidal_request(client, "GET", "/tracks", params=params)
            )
        payload = resp.json()
        included = _included_index(payload)
        return [
            _candidate(resource, included) for resource in _resources_from_payload(payload)[:limit]
        ]

    async def _catalog_access_token(self) -> str | None:
        settings = get_settings()
        if not settings.tidal_client_id or not settings.tidal_client_secret:
            return None
        if self._catalog_token is not None and (
            self._catalog_token.expires_at is None or self._catalog_token.expires_at > time.time()
        ):
            return self._catalog_token.access_token

        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            resp = await _tidal_request(
                client,
                "POST",
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.tidal_client_id,
                },
                auth=_token_auth(settings),
            )
        if not resp.is_success:
            if resp.status_code == 429:
                _raise_for_status(resp)
            logger.warning(
                "tidal client-credentials token request failed status=%s error=%s",
                resp.status_code,
                _tidal_error_message(resp),
            )
            return None
        token = resp.json()
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.warning("tidal client-credentials token response omitted access_token")
            return None
        self._catalog_token = _CatalogToken(
            access_token=access_token,
            expires_at=_expires_at(token.get("expires_in")),
        )
        return access_token

    async def _search_tracks_by_text(
        self, client: httpx.AsyncClient, cred: ProviderCredential, track: Track, *, limit: int
    ) -> list[TrackCandidate]:
        query = f"{track.title} {track.artist}".strip()
        if track.album:
            query = f"{query} {track.album}".strip()
        path = f"/searchResults/{urllib.parse.quote(query, safe='')}/relationships/tracks"
        params = _params(
            cred,
            [
                ("explicitFilter", "INCLUDE"),
                *_include_params("tracks"),
            ],
        )
        resp = _raise_for_status(await _tidal_request(client, "GET", path, params=params))
        payload = resp.json()
        resources = _resources_from_relationship_payload(payload)
        if not resources:
            ids = [
                item.get("id")
                for item in payload.get("data", [])
                if isinstance(item, dict)
                and item.get("type") == "tracks"
                and isinstance(item.get("id"), str)
            ]
            fetched = await self._fetch_tracks(client, cred, ids[:limit])
            resources = [fetched[item_id] for item_id in ids[:limit] if item_id in fetched]
            included = fetched.included
        else:
            included = _included_index(payload)
        return [_candidate(resource, included) for resource in resources[:limit]]

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        track_id = _track_id(uri)
        if not track_id:
            return False
        async with self._client(cred) as client:
            resp = await _tidal_request(client, "GET", f"/tracks/{track_id}", params=_params(cred))
        if resp.status_code == 404:
            return False
        _raise_for_status(resp)
        return True

    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        attrs: dict[str, Any] = {
            "name": spec.name,
            "accessType": "PUBLIC" if spec.public else "UNLISTED",
        }
        if spec.description:
            attrs["description"] = spec.description
        payload = {"data": {"type": "playlists", "attributes": attrs}}
        async with self._client(cred) as client:
            resp = _raise_for_status(
                await _tidal_request(
                    client,
                    "POST",
                    "/playlists",
                    json=payload,
                    params=_params(cred),
                    headers={"Idempotency-Key": uuid.uuid4().hex},
                )
            )
        data = resp.json().get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise ProviderError("tidal create playlist response did not include playlist id")
        return str(data["id"])

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        out: list[AddItemResult] = []
        position = 0
        async with self._client(cred) as client:
            for start in range(0, len(uris), self.info.capabilities.max_add_batch):
                chunk = list(uris[start : start + self.info.capabilities.max_add_batch])
                payload_items = []
                invalid = []
                for uri in chunk:
                    track_id = _track_id(uri)
                    if track_id:
                        payload_items.append({"type": "tracks", "id": track_id})
                    else:
                        invalid.append(uri)
                if invalid:
                    for uri in chunk:
                        if uri in invalid:
                            out.append(
                                AddItemResult(uri=uri, ok=False, error="invalid Tidal track URI")
                            )
                        else:
                            out.append(
                                AddItemResult(
                                    uri=uri,
                                    ok=False,
                                    error="batch contained invalid URI",
                                )
                            )
                    continue
                _raise_for_status(
                    await _tidal_request(
                        client,
                        "POST",
                        f"/playlists/{playlist_id}/relationships/items",
                        json={"data": payload_items},
                        params=_params(cred),
                        headers={"Idempotency-Key": uuid.uuid4().hex},
                    )
                )
                for uri in chunk:
                    out.append(AddItemResult(uri=uri, ok=True, position=position))
                    position += 1
        return out

    async def _fetch_tracks(
        self, client: httpx.AsyncClient, cred: ProviderCredential, ids: Sequence[str]
    ) -> _FetchedTracks:
        resources: dict[str, dict[str, Any]] = {}
        included: dict[tuple[str, str], dict[str, Any]] = {}
        ids = [item_id for item_id in ids if item_id]
        for start in range(0, len(ids), _MAX_ADD_BATCH):
            chunk = ids[start : start + _MAX_ADD_BATCH]
            params = _params(
                cred,
                [
                    *[("filter[id]", item_id) for item_id in chunk],
                    *_include_params(*_TRACK_INCLUDES),
                ],
            )
            resp = _raise_for_status(await _tidal_request(client, "GET", "/tracks", params=params))
            payload = resp.json()
            included.update(_included_index(payload))
            for resource in _resources_from_payload(payload):
                resource_id = _resource_id(resource)
                if resource_id:
                    resources[resource_id] = resource
        return _FetchedTracks(resources, included)


class _FetchedTracks(dict[str, dict[str, Any]]):
    def __init__(
        self,
        resources: dict[str, dict[str, Any]],
        included: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        super().__init__(resources)
        self.included = included


adapter = register(TidalAdapter())
