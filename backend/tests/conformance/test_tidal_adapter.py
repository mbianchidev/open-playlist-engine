"""Tidal adapter specifics beyond the generic provider contract."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    ProviderCredential,
    RateLimited,
    RefreshTokenExpired,
)
from app.core.models import Album, Artist, PlaylistKind, Track
from app.providers.tidal.adapter import (
    _PENDING_STATES,
    TIDAL_COLLECTION_TRACKS_PLAYLIST_ID,
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


def _collection_cred() -> ProviderCredential:
    return _cred().model_copy(
        update={
            "scopes": [
                "collection.read",
                "collection.write",
                "playlists.read",
                "playlists.write",
                "search.read",
                "user.read",
            ]
        }
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


async def test_iter_and_read_my_collection() -> None:
    adapter = TidalAdapter(transport=tidal_transport())
    refs = [ref async for ref in adapter.iter_playlists(_collection_cred())]
    collection = next(ref for ref in refs if ref.id == TIDAL_COLLECTION_TRACKS_PLAYLIST_ID)

    assert collection.name == "My Collection"
    assert collection.track_count == 2
    assert collection.kind is PlaylistKind.LIKED_TRACKS

    playlist = await adapter.read_playlist(_collection_cred(), collection)
    assert playlist.kind is PlaylistKind.LIKED_TRACKS
    assert [track.id for track in playlist.tracks] == ["t1", "t2"]
    assert playlist.tracks[0].artist == "Artist One"


async def test_my_collection_requires_read_scope() -> None:
    adapter = TidalAdapter(transport=tidal_transport())
    with pytest.raises(AccessDenied, match="collection.read"):
        await adapter.read_playlist(
            _cred(),
            _ref(TIDAL_COLLECTION_TRACKS_PLAYLIST_ID, "My Collection"),
        )


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

    assert [ref.id for ref in refs] == ["page-one", TIDAL_COLLECTION_TRACKS_PLAYLIST_ID]
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
    assert scopes == {
        "collection.read",
        "collection.write",
        "playlists.read",
        "playlists.write",
        "search.read",
        "user.read",
    }
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


async def test_add_tracks_to_my_collection_batches_at_limit() -> None:
    calls: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.read())
        assert request.url.path == "/v2/userCollectionTracks/me/relationships/items"
        assert "countryCode" not in request.url.params
        return httpx.Response(200, json={"data": [], "links": {"self": request.url.path}})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    results = await adapter.add_tracks(
        _collection_cred(),
        TIDAL_COLLECTION_TRACKS_PLAYLIST_ID,
        [f"tidal:track:t{i}" for i in range(51)],
    )

    assert len(calls) == 2
    assert calls[0].count(b'"type":"tracks"') == 50
    assert calls[1].count(b'"type":"tracks"') == 1
    assert all(result.ok for result in results)


async def test_add_tracks_to_my_collection_requires_write_scope() -> None:
    adapter = TidalAdapter(transport=tidal_transport())
    with pytest.raises(AccessDenied, match="collection.write"):
        await adapter.add_tracks(
            _cred().model_copy(update={"scopes": ["collection.read"]}),
            TIDAL_COLLECTION_TRACKS_PLAYLIST_ID,
            ["tidal:track:t1"],
        )


async def test_saved_albums_and_favorite_artists_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/userCollectionAlbums/me/relationships/items":
            return httpx.Response(
                200,
                json={
                    "data": [{"type": "albums", "id": "album1"}],
                    "included": [
                        {
                            "type": "albums",
                            "id": "album1",
                            "attributes": {
                                "title": "Album One",
                                "barcodeId": "0123456789012",
                                "releaseDate": "2020-01-02",
                            },
                            "relationships": {
                                "artists": {"data": [{"type": "artists", "id": "artist1"}]}
                            },
                        },
                        {
                            "type": "artists",
                            "id": "artist1",
                            "attributes": {"name": "Artist One", "popularity": 0.8},
                        },
                    ],
                    "links": {"self": path},
                },
            )
        if path == "/v2/userCollectionArtists/me/relationships/items":
            return httpx.Response(
                200,
                json={
                    "data": [{"type": "artists", "id": "artist1"}],
                    "included": [
                        {
                            "type": "artists",
                            "id": "artist1",
                            "attributes": {"name": "Artist One", "popularity": 0.8},
                        }
                    ],
                    "links": {"self": path},
                },
            )
        if path == "/v2/albums/album1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "type": "albums",
                        "id": "album1",
                        "attributes": {
                            "title": "Album One",
                            "barcodeId": "0123456789012",
                            "releaseDate": "2020-01-02",
                        },
                        "relationships": {
                            "artists": {"data": [{"type": "artists", "id": "artist1"}]}
                        },
                    },
                    "included": [
                        {
                            "type": "artists",
                            "id": "artist1",
                            "attributes": {"name": "Artist One", "popularity": 0.8},
                        }
                    ],
                },
            )
        if path == "/v2/artists/artist1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "type": "artists",
                        "id": "artist1",
                        "attributes": {"name": "Artist One", "popularity": 0.8},
                    }
                },
            )
        return httpx.Response(404, json={})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    albums = [album async for album in adapter.iter_saved_albums(_collection_cred())]
    artists = [artist async for artist in adapter.iter_followed_artists(_collection_cred())]

    assert albums[0].title == "Album One"
    assert albums[0].artists == ["Artist One"]
    assert albums[0].upc == "0123456789012"
    assert artists[0].name == "Artist One"
    assert (await adapter.read_saved_album(_collection_cred(), "album1")).id == "album1"
    assert (await adapter.read_followed_artist(_collection_cred(), "artist1")).id == "artist1"


async def test_library_search_uses_tidal_search_relationships() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/relationships/albums"):
            return httpx.Response(
                200,
                json={
                    "data": [{"type": "albums", "id": "album1"}],
                    "included": [
                        {
                            "type": "albums",
                            "id": "album1",
                            "attributes": {
                                "title": "Album One",
                                "barcodeId": "0123456789012",
                                "releaseDate": "2020-01-02",
                            },
                            "relationships": {
                                "artists": {"data": [{"type": "artists", "id": "artist1"}]}
                            },
                        },
                        {
                            "type": "artists",
                            "id": "artist1",
                            "attributes": {"name": "Artist One", "popularity": 0.8},
                        },
                    ],
                },
            )
        if path.endswith("/relationships/artists"):
            return httpx.Response(
                200,
                json={
                    "data": [{"type": "artists", "id": "artist1"}],
                    "included": [
                        {
                            "type": "artists",
                            "id": "artist1",
                            "attributes": {"name": "Artist One", "popularity": 0.8},
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    albums = await adapter.search_albums(
        _collection_cred(),
        Album(title="Album One", artists=["Artist One"]),
    )
    artists = await adapter.search_artists(_collection_cred(), Artist(name="Artist One"))

    assert albums[0].uri == "tidal:album:album1"
    assert artists[0].uri == "tidal:artist:artist1"


async def test_library_contains_and_writes_use_tidal_collection_relationships() -> None:
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.read()))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [{"type": "albums", "id": "album1"}]
                    if "Albums" in request.url.path
                    else [{"type": "artists", "id": "artist1"}],
                    "links": {"self": request.url.path},
                },
            )
        return httpx.Response(200, json={"data": [], "links": {"self": request.url.path}})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))

    assert await adapter.contains_saved_albums(
        _collection_cred(), ["tidal:album:album1", "tidal:album:album2"]
    ) == [True, False]
    assert await adapter.contains_followed_artists(
        _collection_cred(), ["tidal:artist:artist1", "tidal:artist:artist2"]
    ) == [True, False]
    assert all(
        result.ok
        for result in await adapter.save_albums(
            _collection_cred(), ["tidal:album:album2"]
        )
    )
    assert all(
        result.ok
        for result in await adapter.follow_artists(
            _collection_cred(), ["tidal:artist:artist2"]
        )
    )
    assert any(
        path.endswith("/userCollectionAlbums/me/relationships/items")
        for _, path, _ in calls
    )
    assert any(
        path.endswith("/userCollectionArtists/me/relationships/items")
        for _, path, _ in calls
    )


async def test_library_writes_map_tidal_skipped_items_per_entity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [],
                "meta": {
                    "skipped": [
                        {"id": "existing", "reason": "ALREADY_PRESENT"},
                        {"id": "missing", "reason": "NOT_FOUND"},
                    ]
                },
            },
        )

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    results = await adapter.save_albums(
        _collection_cred(),
        ["tidal:album:existing", "tidal:album:missing", "tidal:album:new"],
    )

    assert results[0].already_present is True
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[1].error == "NOT_FOUND"
    assert results[2].ok is True


async def test_library_write_reconciles_duplicate_race_and_retries_absent_items() -> None:
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [{"type": "albums", "id": "existing"}],
                    "links": {"self": request.url.path},
                },
            )
        post_calls += 1
        if post_calls == 1:
            return httpx.Response(
                409,
                json={
                    "errors": [
                        {
                            "code": "DUPLICATE_ITEMS_IN_COLLECTION",
                            "detail": "one item already exists",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"data": []})

    adapter = TidalAdapter(transport=httpx.MockTransport(handler))
    results = await adapter.save_albums(
        _collection_cred(),
        ["tidal:album:existing", "tidal:album:new"],
    )

    assert results[0].already_present is True
    assert results[1].ok is True
    assert post_calls == 2


def _ref(playlist_id: str = "pl_tidal_1", name: str = "Tidal Roadtrip"):
    from app.core.models import PlaylistRef

    return PlaylistRef(id=playlist_id, name=name)
