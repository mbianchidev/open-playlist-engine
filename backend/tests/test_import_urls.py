from __future__ import annotations

import pytest

from app.imports.urls import UnsafePlaylistUrl, resolve_playlist_url


@pytest.mark.parametrize(
    ("url", "provider", "resource_id", "canonical_url"),
    [
        (
            "https://open.spotify.com/intl-it/playlist/37i9dQZF1DXcBWIGoYBM5M?si=secret",
            "spotify",
            "37i9dQZF1DXcBWIGoYBM5M",
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        ),
        (
            "https://music.youtube.com/playlist?list=PL1234567890_AbCd",
            "ytmusic",
            "PL1234567890_AbCd",
            "https://music.youtube.com/playlist?list=PL1234567890_AbCd",
        ),
        (
            "https://music.apple.com/us/playlist/favorites-mix/pl.u-abc123?l=en",
            "applemusic",
            "pl.u-abc123",
            "https://music.apple.com/us/playlist/pl.u-abc123",
        ),
        (
            "https://listen.tidal.com/browse/playlist/0e02b3a8-8dd0-4f0f-a2cf-3f5fdd263c61",
            "tidal",
            "0e02b3a8-8dd0-4f0f-a2cf-3f5fdd263c61",
            "https://tidal.com/browse/playlist/0e02b3a8-8dd0-4f0f-a2cf-3f5fdd263c61",
        ),
    ],
)
def test_provider_playlist_url_shapes(
    url: str, provider: str, resource_id: str, canonical_url: str
) -> None:
    resolved = resolve_playlist_url(url, open_playlist_hosts=set())

    assert resolved.provider == provider
    assert resolved.resource_id == resource_id
    assert resolved.canonical_url == canonical_url


def test_open_playlist_share_url_maps_to_bounded_json_endpoint() -> None:
    resolved = resolve_playlist_url(
        "https://playlists.example/share/road-trip?utm_source=test",
        open_playlist_hosts={"playlists.example"},
    )

    assert resolved.provider == "openplaylist"
    assert resolved.resource_id == "road-trip"
    assert resolved.canonical_url == "https://playlists.example/share/road-trip"
    assert resolved.metadata["fetch_url"] == (
        "https://playlists.example/open-playlists/road-trip"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://localhost/open-playlists/test",
        "https://127.0.0.1/open-playlists/test",
        "https://user:pass@open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://open.spotify.com/album/37i9dQZF1DXcBWIGoYBM5M",
    ],
)
def test_url_resolver_rejects_unsafe_or_unsupported_urls(url: str) -> None:
    with pytest.raises(UnsafePlaylistUrl):
        resolve_playlist_url(url, open_playlist_hosts={"playlists.example"})

