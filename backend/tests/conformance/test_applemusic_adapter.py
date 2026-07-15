"""Apple Music adapter specifics beyond the generic provider contract."""

from __future__ import annotations

from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from app.core.adapter import (
    AuthExpired,
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    ProviderCredential,
    RateLimited,
    TrackRemoval,
    Unsupported,
)
from app.core.models import PlaylistRef, Track
from app.providers.applemusic.adapter import (
    AppleDeveloperTokenProvider,
    AppleMusicAdapter,
    AppleMusicAuth,
    _catalog_song_from_uri,
    _track_from_library_song,
)
from app.settings import get_settings
from tests.conformance.applemusic_fixtures import (
    APPLE_MUSIC_PLAYLIST_ID,
    applemusic_transport,
)


@pytest.fixture(autouse=True)
def clear_settings() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cred(storefront: str = "us") -> ProviderCredential:
    return ProviderCredential(
        account_id="acc",
        provider="applemusic",
        auth_kind=AuthKind.DEVELOPER_USER_TOKEN,
        access_token="music-user-token",
        extra={"storefront": storefront},
    )


def _adapter_returning(status: int, headers: dict[str, str] | None = None) -> AppleMusicAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={}, headers=headers or {})

    return AppleMusicAdapter(
        transport=httpx.MockTransport(handler),
        developer_token="developer-token",
    )


async def test_401_maps_to_auth_expired() -> None:
    adapter = _adapter_returning(401)
    with pytest.raises(AuthExpired):
        [row async for row in adapter.iter_playlists(_cred())]


async def test_429_maps_to_rate_limited_with_retry_after() -> None:
    adapter = _adapter_returning(429, headers={"Retry-After": "31"})
    with pytest.raises(RateLimited) as excinfo:
        await adapter.search_tracks(_cred(), Track(title="x", artist="y"))
    assert excinfo.value.retry_after_s == 31.0


async def test_connection_revalidates_even_with_cached_storefront() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"id": "us", "type": "storefronts"}]})

    adapter = AppleMusicAdapter(
        transport=httpx.MockTransport(handler),
        developer_token="developer-token",
    )
    await adapter.test_connection(_cred())

    assert calls == 1


async def test_read_paginates_and_enriches_library_tracks() -> None:
    adapter = AppleMusicAdapter(
        transport=applemusic_transport(),
        developer_token="developer-token",
    )
    refs = [row async for row in adapter.iter_playlists(_cred())]
    playlist = await adapter.read_playlist(_cred(), refs[0])

    assert [ref.name for ref in refs] == ["Apple Roadtrip", "Focus"]
    assert playlist.id == APPLE_MUSIC_PLAYLIST_ID
    assert playlist.description == "Fixture playlist"
    assert [track.position for track in playlist.tracks] == [0, 1]
    assert [track.isrc for track in playlist.tracks] == ["US0000000001", "US0000000002"]
    assert playlist.tracks[0].provider_uris["applemusic"] == (
        "applemusic:catalog:us:song:1001"
    )
    assert playlist.tracks[0].composer == "Composer One"
    assert playlist.tracks[1].explicit is True


async def test_search_prefers_isrc_then_falls_back_to_text() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return applemusic_transport().handle_request(request)

    adapter = AppleMusicAdapter(
        transport=httpx.MockTransport(handler),
        developer_token="developer-token",
    )
    isrc_results = await adapter.search_tracks(
        _cred(),
        Track(title="Song One", artist="Artist One", isrc="US0000000001"),
    )
    text_results = await adapter.search_tracks(
        _cred(),
        Track(title="Song One", artist="Artist One"),
    )

    assert isrc_results[0].uri == "applemusic:catalog:us:song:1001"
    assert "filter%5Bisrc%5D=US0000000001" in calls[0]
    assert text_results[0].artist == "Artist One"
    assert any("/v1/catalog/us/search" in call for call in calls)


async def test_validate_uri_rejects_missing_and_cross_storefront_tracks() -> None:
    adapter = AppleMusicAdapter(
        transport=applemusic_transport(),
        developer_token="developer-token",
    )
    assert await adapter.validate_uri(_cred(), "applemusic:catalog:us:song:1001")
    assert not await adapter.validate_uri(_cred(), "applemusic:catalog:gb:song:1001")
    assert not await adapter.validate_uri(_cred(), "applemusic:catalog:us:song:missing")


def test_catalog_song_uri_parsing() -> None:
    assert _catalog_song_from_uri("applemusic:catalog:us:song:1001") == ("us", "1001")
    assert _catalog_song_from_uri("applemusic:song:1001") == (None, "1001")
    assert _catalog_song_from_uri("https://music.apple.com/us/album/x/99?i=1001") == (
        "us",
        "1001",
    )
    assert _catalog_song_from_uri("not-a-song") == (None, None)


def test_catalogless_library_song_remains_migratable_by_metadata() -> None:
    track = _track_from_library_song(
        {
            "id": "a.uploaded",
            "type": "library-songs",
            "attributes": {
                "name": "Uploaded Song",
                "artistName": "Local Artist",
                "playParams": {"id": "a.uploaded", "kind": "song", "isLibrary": True},
            },
        },
        storefront="us",
        catalog_by_id={},
        position=0,
    )

    assert track.is_migratable is True
    assert track.isrc is None
    assert track.provider_uris["applemusic"] == "applemusic:library:song:a.uploaded"


async def test_create_playlist_payload_uses_supported_attributes() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content
        return httpx.Response(
            201,
            json={
                "data": [
                    {
                        "id": "p.created",
                        "type": "library-playlists",
                        "attributes": {"name": "Created"},
                    }
                ]
            },
        )

    adapter = AppleMusicAdapter(
        transport=httpx.MockTransport(handler),
        developer_token="developer-token",
    )
    playlist_id = await adapter.create_playlist(
        _cred(),
        CreatePlaylistSpec(name="Created", description="Description", public=True),
    )

    assert playlist_id == "p.created"
    assert captured["json"] == b'{"attributes":{"name":"Created","description":"Description"}}'


async def test_add_tracks_reports_invalid_items_and_preserves_positions() -> None:
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(204)

    adapter = AppleMusicAdapter(
        transport=httpx.MockTransport(handler),
        developer_token="developer-token",
    )
    results = await adapter.add_tracks(
        _cred(),
        "p.existing",
        [
            "applemusic:catalog:us:song:1001",
            "applemusic:catalog:gb:song:2001",
            "https://music.apple.com/us/album/x/99?i=1002",
        ],
    )

    assert [result.ok for result in results] == [True, False, True]
    assert [result.position for result in results] == [0, None, 2]
    assert captured == [
        b'{"data":[{"id":"1001","type":"songs"},{"id":"1002","type":"songs"}]}'
    ]


async def test_new_playlist_add_retries_only_not_found() -> None:
    post_tracks_calls = 0
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_tracks_calls
        if request.url.path == "/v1/me/library/playlists":
            return httpx.Response(
                201,
                json={"data": [{"id": "p.created", "type": "library-playlists"}]},
            )
        post_tracks_calls += 1
        return httpx.Response(404 if post_tracks_calls == 1 else 204, json={})

    adapter = AppleMusicAdapter(
        transport=httpx.MockTransport(handler),
        developer_token="developer-token",
        sleep=fake_sleep,
    )
    playlist_id = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="Created"))
    results = await adapter.add_tracks(
        _cred(),
        playlist_id,
        ["applemusic:catalog:us:song:1001"],
    )

    assert results[0].ok is True
    assert post_tracks_calls == 2
    assert delays == [1]


async def test_auth_begin_generates_es256_developer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_key_pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    ).decode()
    monkeypatch.setenv("OPE_APPLE_MUSIC_TEAM_ID", "TEAMID1234")
    monkeypatch.setenv("OPE_APPLE_MUSIC_KEY_ID", "KEYID12345")
    monkeypatch.setenv("OPE_APPLE_MUSIC_PRIVATE_KEY", private_key_pem)
    monkeypatch.setenv("OPE_APPLE_MUSIC_TOKEN_TTL_S", "3600")
    get_settings.cache_clear()

    auth = AppleMusicAuth(token_provider=AppleDeveloperTokenProvider())
    challenge = await auth.begin(user_id="local")
    field = challenge.form_schema["music_user_token"] if challenge.form_schema else {}
    token = field["developer_token"]
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert challenge.shape is ChallengeShape.FORM
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEYID12345"
    assert claims["iss"] == "TEAMID1234"
    assert 3500 <= claims["exp"] - claims["iat"] <= 3600


async def test_auth_complete_uses_stable_account_identity() -> None:
    auth = AppleMusicAuth(
        token_provider=AppleDeveloperTokenProvider(token="developer-token"),
        transport=applemusic_transport(),
    )
    credential = await auth.complete(
        user_id="local",
        callback={"music_user_token": "music-user-token"},
    )

    assert credential.account_id == "applemusic-user"
    assert credential.access_token == "music-user-token"
    assert credential.expires_at is None
    assert credential.extra["storefront"] == "us"
    assert credential.extra["display_name"] == "Apple Music (US)"


async def test_playlist_removal_operations_are_explicitly_unsupported() -> None:
    adapter = AppleMusicAdapter(
        transport=applemusic_transport(),
        developer_token="fixture-developer-token",
    )
    credential = ProviderCredential(
        account_id="acc",
        provider="applemusic",
        auth_kind=AuthKind.DEVELOPER_USER_TOKEN,
        access_token="fixture-user-token",
        extra={"storefront": "us"},
    )
    playlist = PlaylistRef(id=APPLE_MUSIC_PLAYLIST_ID, name="Roadtrip", is_owned=True)

    with pytest.raises(Unsupported):
        await adapter.unfollow_playlist(credential, playlist)
    with pytest.raises(Unsupported):
        await adapter.delete_playlist(credential, playlist)
    with pytest.raises(Unsupported):
        await adapter.remove_tracks(
            credential,
            playlist,
            [
                TrackRemoval(
                    source_item_id="library-song",
                    provider_uri="applemusic:catalog:us:song:1001",
                    position=0,
                )
            ],
        )
