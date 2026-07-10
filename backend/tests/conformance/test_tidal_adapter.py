"""Tidal adapter specifics beyond the generic provider contract."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.adapter import (
    AuthExpired,
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    ProviderCredential,
    RateLimited,
    RefreshTokenExpired,
)
from app.core.models import Track
from app.providers.tidal.adapter import (
    _PENDING_STATES,
    TidalAdapter,
    TidalAuth,
    _track_id,
)
from app.settings import get_settings
from tests.conformance.tidal_fixtures import tidal_transport


@pytest.fixture(autouse=True)
def clear_auth_state() -> None:
    get_settings.cache_clear()
    _PENDING_STATES.clear()
    yield
    get_settings.cache_clear()
    _PENDING_STATES.clear()


def _cred() -> ProviderCredential:
    return ProviderCredential(
        account_id="acc",
        provider="tidal",
        auth_kind=AuthKind.OAUTH_PKCE,
        access_token="token",
        extra={"country": "US"},
    )


def _adapter_returning(status: int, headers: dict[str, str] | None = None) -> TidalAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={}, headers=headers or {})

    return TidalAdapter(transport=httpx.MockTransport(handler))


def _form(request: httpx.Request) -> dict[str, str]:
    values = parse_qs(request.content.decode())
    return {key: value[-1] for key, value in values.items()}


async def test_401_maps_to_auth_expired() -> None:
    adapter = _adapter_returning(401)
    with pytest.raises(AuthExpired):
        [r async for r in adapter.iter_playlists(_cred())]


async def test_429_maps_to_rate_limited_with_retry_after() -> None:
    adapter = _adapter_returning(429, headers={"Retry-After": "12"})
    with pytest.raises(RateLimited) as excinfo:
        await adapter.search_tracks(_cred(), Track(title="x", artist="y"))
    assert excinfo.value.retry_after_s == 12.0
    assert str(excinfo.value) == "tidal rate limited; retry after 12 seconds"


async def test_read_hydrates_shallow_items_and_preserves_positions() -> None:
    adapter = TidalAdapter(transport=tidal_transport())
    pl = await adapter.read_playlist(_cred(), ref=_ref())
    assert pl.name == "Tidal Roadtrip"
    assert pl.description == "Fixture playlist"
    assert pl.owner_id == "tidal-user-1"
    assert [t.position for t in pl.tracks] == [0, 1]
    assert pl.tracks[0].isrc == "US0000000001"
    assert pl.tracks[0].provider_uris["tidal"] == "tidal:track:t1"
    assert pl.tracks[0].artist == "Artist One"
    assert pl.tracks[0].album == "Album One"
    assert pl.tracks[0].duration_s == 180
    assert pl.tracks[0].release_year == 2020
    assert pl.tracks[0].credits[0].role == "Vocals"
    assert pl.tracks[1].artist == "Artist Two, Artist Three"


async def test_playlist_pagination_keeps_v2_base_path() -> None:
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("page[cursor]")
        requests.append((request.url.path, cursor))
        if cursor == "next":
            return httpx.Response(
                200,
                json={"data": [], "links": {"self": "/playlists?page[cursor]=next"}},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "page-one",
                        "type": "playlists",
                        "attributes": {"name": "Page One", "numberOfItems": 0},
                    }
                ],
                "links": {
                    "self": "/playlists",
                    "next": "/playlists?page[cursor]=next",
                },
            },
        )

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    refs = [ref async for ref in adapter.iter_playlists(_cred())]

    assert [ref.id for ref in refs] == ["page-one"]
    assert requests == [("/v2/playlists", None), ("/v2/playlists", "next")]


async def test_search_prefers_isrc_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPE_TIDAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("OPE_TIDAL_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.tidal.com":
            return httpx.Response(200, json={"access_token": "catalog-token", "expires_in": 3600})
        calls.append(
            {
                "path": request.url.path,
                "isrc": request.url.params.get("filter[isrc]", ""),
            }
        )
        return httpx.Response(200, json={"data": [], "links": {"self": request.url.path}})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    await adapter.search_tracks(_cred(), Track(title="t", artist="a", isrc="US0000000001"))

    assert calls[0]["path"] == "/v2/tracks"
    assert calls[0]["isrc"] == "US0000000001"


async def test_search_falls_back_to_text_relationship() -> None:
    adapter = TidalAdapter(transport=tidal_transport())
    results = await adapter.search_tracks(_cred(), Track(title="Song One", artist="Artist One"))
    assert results
    assert results[0].uri == "tidal:track:t1"
    assert results[0].artist == "Artist One"
    assert results[0].album == "Album One"


async def test_validate_uri_true_and_false() -> None:
    adapter = TidalAdapter(transport=tidal_transport())
    assert await adapter.validate_uri(_cred(), "tidal:track:t1") is True
    assert await adapter.validate_uri(_cred(), "tidal:track:missing") is False


def test_track_id_parsing() -> None:
    assert _track_id("tidal:track:abc") == "abc"
    assert _track_id("https://tidal.com/browse/track/abc?u") == "abc"
    assert _track_id("https://listen.tidal.com/track/abc") == "abc"
    assert _track_id("abc") == "abc"


async def test_create_playlist_payload_and_visibility() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read()
        captured["content_type"] = request.headers.get("content-type")
        captured["idempotency"] = request.headers.get("idempotency-key")
        return httpx.Response(
            201,
            json={
                "data": {
                    "id": "created",
                    "type": "playlists",
                    "attributes": {"name": "Created"},
                },
                "links": {"self": "/playlists/created"},
            },
        )

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    playlist_id = await adapter.create_playlist(
        _cred(), CreatePlaylistSpec(name="Created", description="Desc")
    )

    assert playlist_id == "created"
    assert captured["content_type"] == "application/vnd.api+json"
    assert captured["idempotency"]
    assert b'"accessType":"UNLISTED"' in captured["payload"]
    assert b'"description":"Desc"' in captured["payload"]


async def test_add_tracks_batches_at_tidal_limit() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        calls.append([part.split('"', 1)[0] for part in payload.split('"id":"')[1:]])
        return httpx.Response(200, json={"data": [], "links": {"self": request.url.path}})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    uris = [f"tidal:track:t{i}" for i in range(51)]
    results = await adapter.add_tracks(_cred(), "playlist", uris)

    assert [len(call) for call in calls] == [50, 1]
    assert [r.position for r in results] == list(range(51))
    assert all(r.ok for r in results)


async def test_auth_begin_uses_third_party_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPE_TIDAL_CLIENT_ID", "client-id")
    get_settings.cache_clear()

    challenge = await TidalAuth().begin(user_id="local")
    params = parse_qs(urlparse(challenge.redirect_url or "").query)

    assert challenge.shape is ChallengeShape.REDIRECT
    assert params["client_id"] == ["client-id"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8000/api/auth/tidal/callback"]
    scopes = set(params["scope"][0].split())
    assert scopes == {"playlists.read", "playlists.write", "search.read", "user.read"}
    assert "r_usr" not in scopes
    assert "w_usr" not in scopes
    assert challenge.state in _PENDING_STATES


async def test_auth_complete_persists_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPE_TIDAL_CLIENT_ID", "client-id")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.tidal.com":
            data = _form(request)
            assert data["grant_type"] == "authorization_code"
            assert data["client_id"] == "client-id"
            assert data["code"] == "auth-code"
            assert data["code_verifier"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "scope": "playlists.read playlists.write search.read user.read",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "tidal-user-1",
                    "type": "users",
                    "attributes": {
                        "username": "tidal_user",
                        "email": "tidal@example.com",
                        "country": "US",
                    },
                },
                "links": {"self": "/users/me"},
            },
        )

    auth = TidalAuth(transport=httpx.MockTransport(handler))
    challenge = await auth.begin(user_id="local")
    cred = await auth.complete(
        user_id="local", callback={"state": challenge.state, "code": "auth-code"}
    )

    assert cred.provider == "tidal"
    assert cred.account_id == "tidal-user-1"
    assert cred.access_token == "access-token"
    assert cred.refresh_token == "refresh-token"
    assert cred.extra["display_name"] == "tidal_user"
    assert cred.extra["country"] == "US"
    assert challenge.state not in _PENDING_STATES


async def test_refresh_invalid_grant_requires_reauthorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPE_TIDAL_CLIENT_ID", "client-id")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    cred = ProviderCredential(
        account_id="a",
        provider="tidal",
        auth_kind=AuthKind.OAUTH_PKCE,
        refresh_token="expired-refresh-token",
    )

    with pytest.raises(RefreshTokenExpired):
        await TidalAuth(transport=httpx.MockTransport(handler)).refresh(cred)


def _ref():
    from app.core.models import PlaylistRef

    return PlaylistRef(id="pl_tidal_1", name="Tidal Roadtrip")
