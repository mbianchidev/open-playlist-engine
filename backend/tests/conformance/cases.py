"""Conformance cases — one per adapter, declaring which behaviours are in scope.

The same contract suite (``test_adapter_contract.py``) runs against every case.
A case advertises the capabilities it exercises (``reads`` / ``searches`` /
``writes``) plus the data the suite needs, so the fake covers the whole contract
while real adapters are driven only over the surface this PR implements:

* Spotify — READ + SEARCH (against recorded HTTP fixtures).
* Tidal — READ + SEARCH + WRITE (against recorded HTTP fixtures).
* YouTube Music — READ + WRITE (against an injected in-memory client).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.adapter import AuthKind, CreatePlaylistSpec, ProviderAdapter, ProviderCredential
from app.core.models import PlaylistRef
from app.providers.spotify.adapter import SpotifyAdapter
from app.providers.tidal.adapter import TidalAdapter
from app.providers.ytmusic.adapter import YTMusicAdapter
from tests.conformance.fake_provider import FakeAdapter, fake_cred
from tests.conformance.spotify_fixtures import SPOTIFY_PLAYLIST_ID, spotify_transport
from tests.conformance.tidal_fixtures import TIDAL_PLAYLIST_ID, tidal_transport
from tests.conformance.ytmusic_fakes import FakeYTMusic


@dataclass
class Case:
    id: str
    adapter: ProviderAdapter
    cred: ProviderCredential

    # READ + SEARCH
    reads: bool = False
    searches: bool = False
    expect_isrc: bool = False
    missing_ref: PlaylistRef = field(
        default_factory=lambda: PlaylistRef(id="missing", name="missing")
    )
    search_title: str = "Song One"
    search_artist: str = "Artist One"
    search_uri_prefix: str = ""

    # WRITE
    writes: bool = False
    create_spec: CreatePlaylistSpec | None = None
    add_uris: list[str] = field(default_factory=list)


def _fake_case() -> Case:
    return Case(
        id="fake",
        adapter=FakeAdapter(),
        cred=fake_cred("fake"),
        reads=True,
        searches=True,
        expect_isrc=True,
        search_uri_prefix="fake:track:",
        writes=True,
        create_spec=CreatePlaylistSpec(name="Mirror"),
        add_uris=["fake:track:Song One", "fake:track:Song Two"],
    )


def _spotify_case() -> Case:
    return Case(
        id="spotify",
        adapter=SpotifyAdapter(transport=spotify_transport()),
        cred=ProviderCredential(
            account_id="acc-spotify",
            provider="spotify",
            auth_kind=AuthKind.OAUTH_PKCE,
            access_token="fixture-token",
        ),
        reads=True,
        searches=True,
        expect_isrc=True,
        missing_ref=PlaylistRef(id="missing", name="missing"),
        search_uri_prefix="spotify:track:",
    )


def _ytmusic_case() -> Case:
    fake = FakeYTMusic()
    created = fake.create_playlist("Mirror", "", "PRIVATE", ["aaa111", "bbb222"])
    return Case(
        id="ytmusic",
        adapter=YTMusicAdapter(client_factory=lambda cred: fake),
        cred=ProviderCredential(
            account_id="acc-ytmusic",
            provider="ytmusic",
            auth_kind=AuthKind.OAUTH_DEVICE,
            access_token="fixture-token",
        ),
        reads=True,
        writes=True,
        missing_ref=PlaylistRef(id="missing", name="missing"),
        create_spec=CreatePlaylistSpec(name=f"Mirror {created}"),
        add_uris=[
            "https://music.youtube.com/watch?v=aaa111",
            "https://music.youtube.com/watch?v=bbb222",
        ],
    )


def _tidal_case() -> Case:
    return Case(
        id="tidal",
        adapter=TidalAdapter(transport=tidal_transport()),
        cred=ProviderCredential(
            account_id="acc-tidal",
            provider="tidal",
            auth_kind=AuthKind.OAUTH_PKCE,
            access_token="fixture-token",
            extra={"country": "US"},
        ),
        reads=True,
        searches=True,
        expect_isrc=True,
        writes=True,
        missing_ref=PlaylistRef(id="missing", name="missing"),
        search_uri_prefix="tidal:track:",
        create_spec=CreatePlaylistSpec(name="Mirror"),
        add_uris=["tidal:track:t1", "https://tidal.com/browse/track/t2"],
    )


def build_cases() -> list[Case]:
    return [_fake_case(), _spotify_case(), _ytmusic_case(), _tidal_case()]


# Used by the suite to keep the original FakeAdapter-only test (provider fixture
# constants are re-exported for provider-specific assertions).
__all__ = ["Case", "build_cases", "SPOTIFY_PLAYLIST_ID", "TIDAL_PLAYLIST_ID"]
