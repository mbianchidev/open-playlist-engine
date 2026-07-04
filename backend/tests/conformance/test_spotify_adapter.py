"""Spotify adapter specifics beyond the generic contract: typed-error mapping,
ISRC-first search, fidelity flags and URI parsing."""

from __future__ import annotations

import httpx
import pytest

from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    AuthKind,
    ProviderCredential,
    RateLimited,
    RefreshTokenExpired,
)
from app.core.models import MediaType, Track
from app.providers.spotify.adapter import (
    SPOTIFY_SAVED_TRACKS_PLAYLIST_ID,
    SpotifyAdapter,
    SpotifyAuth,
    _track_id,
)
from app.settings import get_settings
from tests.conformance.spotify_fixtures import SPOTIFY_PLAYLIST_ID, spotify_transport


def _cred() -> ProviderCredential:
    return ProviderCredential(
        account_id="acc",
        provider="spotify",
        auth_kind=AuthKind.OAUTH_PKCE,
        access_token="token",
    )


def _adapter_returning(status: int, headers: dict | None = None) -> SpotifyAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={}, headers=headers or {})

    return SpotifyAdapter(transport=httpx.MockTransport(handler))


async def test_401_maps_to_auth_expired() -> None:
    adapter = _adapter_returning(401)
    with pytest.raises(AuthExpired):
        [r async for r in adapter.iter_playlists(_cred())]


async def test_429_without_retry_after_maps_to_rate_limited() -> None:
    adapter = _adapter_returning(429)
    with pytest.raises(RateLimited) as excinfo:
        await adapter.search_tracks(_cred(), Track(title="x", artist="y"))
    assert excinfo.value.retry_after_s is None
    assert excinfo.value.status_code == 429
    assert str(excinfo.value) == "spotify rate limited"


async def test_429_with_retry_after_retries_before_returning() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={}, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "target",
                            "name": "Target Song",
                            "uri": "spotify:track:target",
                            "artists": [{"name": "Target Artist"}],
                            "duration_ms": 123000,
                        }
                    ]
                }
            },
        )

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    results = await adapter.search_tracks(
        _cred(), Track(title="Target Song", artist="Target Artist")
    )

    assert calls == 2
    assert results[0].uri == "spotify:track:target"


async def test_429_with_large_retry_after_surfaces_rate_limit() -> None:
    adapter = _adapter_returning(429, headers={"Retry-After": "50963"})
    with pytest.raises(RateLimited) as excinfo:
        await adapter.search_tracks(_cred(), Track(title="x", artist="y"))
    assert excinfo.value.retry_after_s == 50963.0
    assert excinfo.value.status_code == 429
    assert str(excinfo.value) == "spotify rate limited; retry after 50963 seconds"


async def test_420_maps_to_rate_limited_with_status_preserved() -> None:
    adapter = _adapter_returning(420)
    with pytest.raises(RateLimited) as excinfo:
        [r async for r in adapter.iter_playlists(_cred())]
    assert excinfo.value.status_code == 420
    assert str(excinfo.value) == "spotify rate limited"


async def test_missing_access_token_raises_auth_expired() -> None:
    adapter = SpotifyAdapter(transport=spotify_transport())
    cred = ProviderCredential(account_id="a", provider="spotify", auth_kind=AuthKind.OAUTH_PKCE)
    with pytest.raises(AuthExpired):
        [r async for r in adapter.iter_playlists(cred)]


async def test_refresh_invalid_grant_requires_reauthorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPE_SPOTIFY_CLIENT_ID", "client-id")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Refresh token has expired",
            },
        )

    cred = ProviderCredential(
        account_id="a",
        provider="spotify",
        auth_kind=AuthKind.OAUTH_PKCE,
        refresh_token="expired-refresh-token",
    )

    with pytest.raises(RefreshTokenExpired):
        await SpotifyAuth(transport=httpx.MockTransport(handler)).refresh(cred)

    get_settings.cache_clear()


async def test_read_maps_isrc_and_provider_uri_and_position() -> None:
    adapter = SpotifyAdapter(transport=spotify_transport())
    pl = await adapter.read_playlist(
        _cred(), ref=_ref()
    )
    assert pl.name == "Roadtrip"
    assert [t.position for t in pl.tracks] == [0, 1]
    assert pl.tracks[0].isrc == "US0000000001"
    assert pl.tracks[0].provider_uris["spotify"] == "spotify:track:t1"
    assert pl.tracks[0].release_year == 2020
    assert pl.tracks[0].release_date is not None
    assert pl.tracks[0].artwork_uri == "https://img.example.com/album-one.jpg"
    assert pl.tracks[0].explicit is False
    assert pl.tracks[0].credits[0].name == "Artist One"
    # Multiple artists are joined.
    assert pl.tracks[1].artist == "Artist Two, Artist Three"
    assert pl.tracks[1].release_date is None
    assert pl.tracks[1].release_year == 2020
    assert all(t.media_type is MediaType.TRACK for t in pl.tracks)


async def test_iter_playlists_includes_liked_songs_owned_by_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/playlists"):
            return httpx.Response(200, json={"items": [], "next": None})
        if request.url.path.endswith("/me/tracks"):
            return httpx.Response(200, json={"items": [], "total": 2, "next": None})
        return httpx.Response(404, json={})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    refs = [
        ref
        async for ref in adapter.iter_playlists(
            _cred().model_copy(
                update={
                    "scopes": ["user-library-read"],
                    "extra": {"provider_user_id": "spotify-user"},
                }
            )
        )
    ]

    liked = next(ref for ref in refs if ref.id == SPOTIFY_SAVED_TRACKS_PLAYLIST_ID)
    assert liked.name == "Liked Songs"
    assert liked.track_count == 2
    assert liked.owner_id == "spotify-user"
    assert liked.collaborative is False
    assert liked.snapshot_id == "spotify:saved-tracks:total:2"
    assert liked.tracks_href == "/me/tracks"


async def test_read_liked_songs_uses_saved_tracks_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/tracks"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "added_at": "2026-01-01T00:00:00Z",
                            "track": {
                                "id": "liked1",
                                "name": "Liked Song",
                                "uri": "spotify:track:liked1",
                                "type": "track",
                                "duration_ms": 123000,
                                "artists": [{"name": "Liked Artist"}],
                                "album": {"name": "Liked Album"},
                                "external_ids": {"isrc": "US0000000002"},
                            },
                        }
                    ],
                    "total": 1,
                    "next": None,
                },
            )
        return httpx.Response(404, json={})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    playlist = await adapter.read_playlist(
        _cred().model_copy(update={"extra": {"provider_user_id": "spotify-user"}}),
        _ref(SPOTIFY_SAVED_TRACKS_PLAYLIST_ID, "Liked Songs"),
    )

    assert playlist.id == SPOTIFY_SAVED_TRACKS_PLAYLIST_ID
    assert playlist.name == "Liked Songs"
    assert playlist.owner_id == "spotify-user"
    assert playlist.snapshot_id == "spotify:saved-tracks:total:1"
    assert len(playlist.tracks) == 1
    assert playlist.tracks[0].title == "Liked Song"
    assert playlist.tracks[0].provider_uris["spotify"] == "spotify:track:liked1"
    assert playlist.tracks[0].isrc == "US0000000002"


async def test_read_liked_songs_forbidden_prompts_spotify_reconnect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/tracks"):
            return httpx.Response(
                403,
                json={"error": {"status": 403, "message": "Insufficient client scope"}},
            )
        return httpx.Response(404, json={})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))

    with pytest.raises(AccessDenied) as excinfo:
        await adapter.read_playlist(_cred(), _ref(SPOTIFY_SAVED_TRACKS_PLAYLIST_ID, "Liked Songs"))

    assert "reconnect Spotify" in str(excinfo.value)
    assert "saved songs" in str(excinfo.value)


async def test_read_supports_playlist_item_field_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tracks"):
            return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
        return httpx.Response(
            200,
            json={
                "id": SPOTIFY_PLAYLIST_ID,
                "name": "Root Items",
                "description": "",
                "items": {
                    "items": [
                        {
                            "added_at": "2026-01-01T00:00:00Z",
                            "is_local": False,
                            "item": {
                                "id": "new1",
                                "name": "New Shape Song",
                                "uri": "spotify:track:new1",
                                "type": "track",
                                "duration_ms": 123000,
                                "artists": [{"name": "New Artist"}],
                                "album": {"name": "New Album"},
                            },
                        }
                    ],
                    "next": None,
                },
            },
        )

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    pl = await adapter.read_playlist(_cred(), _ref())
    assert pl.name == "Root Items"
    assert len(pl.tracks) == 1
    assert pl.tracks[0].title == "New Shape Song"
    assert pl.tracks[0].provider_uris["spotify"] == "spotify:track:new1"


async def test_read_saved_playlist_falls_back_when_metadata_is_bad_request() -> None:
    playlist_id = "saved-by-other"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/playlists"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": playlist_id,
                            "name": "Saved from someone",
                            "owner": {"id": "other-user"},
                            "tracks": {
                                "total": 1,
                                "href": (
                                    f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
                                    "?saved_ref=1"
                                ),
                            },
                        }
                    ],
                    "next": None,
                },
            )
        if (
            request.url.path.endswith(f"/playlists/{playlist_id}/tracks")
            and request.url.params.get("saved_ref") == "1"
        ):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "added_at": "2026-01-01T00:00:00Z",
                            "is_local": False,
                            "track": {
                                "id": "shared1",
                                "name": "Shared Song",
                                "uri": "spotify:track:shared1",
                                "type": "track",
                                "duration_ms": 123000,
                                "artists": [{"name": "Shared Artist"}],
                                "album": {"name": "Shared Album"},
                            },
                        }
                    ],
                    "next": None,
                },
            )
        if request.url.path.endswith(f"/playlists/{playlist_id}/tracks"):
            return httpx.Response(
                400,
                json={"error": {"status": 400, "message": "Invalid playlist id"}},
            )
        if request.url.path.endswith(f"/playlists/{playlist_id}"):
            return httpx.Response(
                400,
                json={"error": {"status": 400, "message": "Invalid playlist id"}},
            )
        return httpx.Response(404, json={})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    pl = await adapter.read_playlist(_cred(), _ref(playlist_id, "Fallback name"))

    assert pl.id == playlist_id
    assert pl.name == "Saved from someone"
    assert pl.owner_id == "other-user"
    assert len(pl.tracks) == 1
    assert pl.tracks[0].title == "Shared Song"


async def test_read_playlist_uses_tracks_href_when_metadata_track_items_are_empty() -> None:
    playlist_id = "shared-empty-page"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/playlists/{playlist_id}/tracks"):
            if request.url.params.get("from_meta") != "1":
                return httpx.Response(
                    400,
                    json={"error": {"status": 400, "message": "Invalid playlist id"}},
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "added_at": "2026-01-01T00:00:00Z",
                            "is_local": False,
                            "track": {
                                "id": "shared2",
                                "name": "Href Song",
                                "uri": "spotify:track:shared2",
                                "type": "track",
                                "duration_ms": 123000,
                                "artists": [{"name": "Href Artist"}],
                                "album": {"name": "Href Album"},
                            },
                        }
                    ],
                    "next": None,
                },
            )
        if request.url.path.endswith(f"/playlists/{playlist_id}"):
            return httpx.Response(
                200,
                json={
                    "id": playlist_id,
                    "name": "Shared empty page",
                    "tracks": {
                        "href": (
                            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
                            "?from_meta=1"
                        ),
                        "items": [],
                        "total": 1,
                    },
                },
            )
        return httpx.Response(404, json={})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    pl = await adapter.read_playlist(_cred(), _ref(playlist_id, "Shared empty page"))

    assert pl.id == playlist_id
    assert len(pl.tracks) == 1
    assert pl.tracks[0].title == "Href Song"


async def test_forbidden_playlist_tracks_explains_spotify_owner_limit() -> None:
    playlist_id = "external-playlist"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/playlists/{playlist_id}/tracks"):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "status": 403,
                        "message": "Forbidden",
                    }
                },
            )
        if request.url.path.endswith(f"/playlists/{playlist_id}"):
            return httpx.Response(
                200,
                json={
                    "id": playlist_id,
                    "name": "External playlist",
                    "tracks": {
                        "href": f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                        "items": [],
                        "total": 12,
                    },
                },
            )
        return httpx.Response(404, json={})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))

    with pytest.raises(AccessDenied) as excinfo:
        await adapter.read_playlist(_cred(), _ref(playlist_id, "External playlist"))

    message = str(excinfo.value)
    assert "Spotify does not allow this app to read tracks from playlists you do not own" in message
    assert "Add to other playlist" in message
    assert "Delta migration is not available" in message


async def test_search_prefers_isrc_query() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q", "")
        return httpx.Response(200, json={"tracks": {"items": []}})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    await adapter.search_tracks(_cred(), Track(title="t", artist="a", isrc="US0000000001"))
    assert captured["q"] == "isrc:US0000000001"


async def test_validate_uri_true_and_false() -> None:
    adapter = SpotifyAdapter(transport=spotify_transport())
    assert await adapter.validate_uri(_cred(), "spotify:track:t1") is True
    assert await adapter.validate_uri(_cred(), "spotify:track:missing") is False


def test_track_id_parsing() -> None:
    assert _track_id("spotify:track:abc") == "abc"
    assert _track_id("https://open.spotify.com/track/abc?si=1") == "abc"
    assert _track_id("abc") == "abc"


def _ref(playlist_id: str = SPOTIFY_PLAYLIST_ID, name: str = "Roadtrip"):
    from app.core.models import PlaylistRef

    return PlaylistRef(id=playlist_id, name=name)


async def test_local_file_item_is_flagged_not_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tracks"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "is_local": True,
                            "track": {
                                "id": "loc1",
                                "name": "Home Recording",
                                "type": "track",
                                "is_local": True,
                                "artists": [{"name": "Me"}],
                                "duration_ms": 1000,
                            },
                        }
                    ],
                    "next": None,
                },
            )
        return httpx.Response(200, json={"id": SPOTIFY_PLAYLIST_ID, "name": "L"})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    pl = await adapter.read_playlist(_cred(), _ref())
    assert len(pl.tracks) == 1
    item = pl.tracks[0]
    assert item.media_type is MediaType.LOCAL_FILE
    assert item.is_migratable is False
    assert item.unsupported_reason
