"""Spotify adapter specifics beyond the generic contract: typed-error mapping,
ISRC-first search, fidelity flags and URI parsing."""

from __future__ import annotations

import httpx
import pytest

from app.core.adapter import (
    AuthExpired,
    AuthKind,
    ProviderCredential,
    RateLimited,
)
from app.core.models import MediaType, Track
from app.providers.spotify.adapter import SpotifyAdapter, _track_id
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


async def test_429_maps_to_rate_limited_with_retry_after() -> None:
    adapter = _adapter_returning(429, headers={"Retry-After": "7"})
    with pytest.raises(RateLimited) as excinfo:
        await adapter.search_tracks(_cred(), Track(title="x", artist="y"))
    assert excinfo.value.retry_after_s == 7.0


async def test_missing_access_token_raises_auth_expired() -> None:
    adapter = SpotifyAdapter(transport=spotify_transport())
    cred = ProviderCredential(account_id="a", provider="spotify", auth_kind=AuthKind.OAUTH_PKCE)
    with pytest.raises(AuthExpired):
        [r async for r in adapter.iter_playlists(cred)]


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


def _ref():
    from app.core.models import PlaylistRef

    return PlaylistRef(id=SPOTIFY_PLAYLIST_ID, name="Roadtrip")


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
