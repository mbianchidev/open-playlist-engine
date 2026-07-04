"""YouTube Music adapter specifics beyond the generic contract: videoId parsing,
privacy mapping, batching across ``max_add_batch`` and failure reporting."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.adapter import (
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    NotFound,
    ProviderCredential,
    ProviderError,
)
from app.core.models import PlaylistRef, Track
from app.providers.ytmusic.adapter import (
    _PENDING_DEVICE_CODES,
    YTMusicAdapter,
    YTMusicAuth,
    _video_id,
)
from app.settings import get_settings
from tests.conformance.ytmusic_fakes import FakeYTMusic


@pytest.fixture(autouse=True)
def clear_auth_state() -> None:
    get_settings.cache_clear()
    _PENDING_DEVICE_CODES.clear()
    yield
    get_settings.cache_clear()
    _PENDING_DEVICE_CODES.clear()


def _cred() -> ProviderCredential:
    return ProviderCredential(
        account_id="acc",
        provider="ytmusic",
        auth_kind=AuthKind.OAUTH_DEVICE,
        access_token="token",
    )


def _adapter(client: Any) -> YTMusicAdapter:
    return YTMusicAdapter(client_factory=lambda cred: client)


def _oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPE_YTMUSIC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OPE_YTMUSIC_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()


def _form(request: httpx.Request) -> dict[str, str]:
    values = parse_qs(request.content.decode())
    return {key: value[-1] for key, value in values.items()}


def test_video_id_parsing() -> None:
    assert _video_id("https://music.youtube.com/watch?v=abc123") == "abc123"
    assert _video_id("https://www.youtube.com/watch?v=abc123&list=PL1") == "abc123"
    assert _video_id("ytmusic:video:abc123") == "abc123"
    assert _video_id("abc123") == "abc123"


async def test_device_auth_begin_is_default_when_oauth_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oauth_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        data = _form(request)
        assert data["client_id"] == "client-id"
        assert data["scope"] == "https://www.googleapis.com/auth/youtube"
        return httpx.Response(
            200,
            json={
                "device_code": "device-code",
                "user_code": "ABC-123",
                "verification_url": "https://www.google.com/device",
                "expires_in": 1800,
                "interval": 7,
            },
        )

    challenge = await YTMusicAuth(transport=httpx.MockTransport(handler)).begin(user_id="local")

    assert challenge.shape is ChallengeShape.DEVICE_CODE
    assert challenge.user_code == "ABC-123"
    assert challenge.verification_url == "https://www.google.com/device"
    assert challenge.poll_interval_s == 7
    assert challenge.state in _PENDING_DEVICE_CODES


async def test_device_auth_complete_polls_until_token_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oauth_env(monkeypatch)
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        data = _form(request)
        if request.url.path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "device-code",
                    "user_code": "ABC-123",
                    "verification_url": "https://www.google.com/device",
                    "expires_in": 1800,
                    "interval": 1,
                },
            )
        token_calls += 1
        assert data["client_id"] == "client-id"
        assert data["client_secret"] == "client-secret"
        assert data["code"] == "device-code"
        if token_calls == 1:
            return httpx.Response(428, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
            },
        )

    auth = YTMusicAuth(transport=httpx.MockTransport(handler))
    challenge = await auth.begin(user_id="local")

    with pytest.raises(ProviderError, match="authorization_pending"):
        await auth.complete(user_id="local", callback={"state": challenge.state})

    cred = await auth.complete(user_id="local", callback={"state": challenge.state})

    assert cred.auth_kind is AuthKind.OAUTH_DEVICE
    assert cred.access_token == "access-token"
    assert cred.refresh_token == "refresh-token"
    assert cred.scopes == ["https://www.googleapis.com/auth/youtube"]
    assert cred.extra["auth"]["token_type"] == "Bearer"
    assert challenge.state not in _PENDING_DEVICE_CODES


async def test_device_auth_refresh_updates_oauth_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _oauth_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        data = _form(request)
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh-token"
        return httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
            },
        )

    cred = ProviderCredential(
        account_id="acc",
        provider="ytmusic",
        auth_kind=AuthKind.OAUTH_DEVICE,
        access_token="old-access-token",
        refresh_token="refresh-token",
        extra={
            "auth": {
                "access_token": "old-access-token",
                "refresh_token": "refresh-token",
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
                "expires_at": 1,
                "expires_in": 3600,
            }
        },
    )

    refreshed = await YTMusicAuth(transport=httpx.MockTransport(handler)).refresh(cred)

    assert refreshed.access_token == "new-access-token"
    assert refreshed.refresh_token == "refresh-token"
    assert refreshed.extra["auth"]["access_token"] == "new-access-token"


async def test_header_auth_is_self_host_fallback_when_oauth_is_not_configured() -> None:
    challenge = await YTMusicAuth().begin(user_id="local")

    assert challenge.shape is ChallengeShape.FORM
    assert challenge.form_schema and "headers_raw" in challenge.form_schema


async def test_create_playlist_maps_privacy() -> None:
    fake = FakeYTMusic()
    adapter = _adapter(fake)
    pid = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="Pub", public=True))
    assert fake.playlists[pid]["privacy"] == "PUBLIC"
    pid2 = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="Priv"))
    assert fake.playlists[pid2]["privacy"] == "PRIVATE"


async def test_read_playlist_maps_missing_contents_parser_error_to_not_found() -> None:
    class MissingContents:
        def get_playlist(self, *a, **k):
            raise KeyError(
                "Unable to find 'contents' using path "
                "['contents', 'twoColumnBrowseResultsRenderer']"
            )

    adapter = _adapter(MissingContents())

    with pytest.raises(NotFound, match="unavailable or no longer accessible"):
        await adapter.read_playlist(_cred(), PlaylistRef(id="VLPLZFL31xAfxGg", name="Broken"))


async def test_add_tracks_persists_video_ids() -> None:
    fake = FakeYTMusic()
    adapter = _adapter(fake)
    pid = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="M"))
    await adapter.add_tracks(
        _cred(), pid, ["https://music.youtube.com/watch?v=aaa", "ytmusic:video:bbb"]
    )
    assert fake.playlists[pid]["tracks"] == ["aaa", "bbb"]


async def test_add_tracks_batches_and_keeps_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeYTMusic()
    calls: list[int] = []

    original = fake.add_playlist_items

    def spy(playlist_id: str, video_ids=None, source_playlist=None, duplicates=False):
        calls.append(len(video_ids or []))
        return original(playlist_id, video_ids, source_playlist, duplicates)

    fake.add_playlist_items = spy  # type: ignore[method-assign]
    adapter = YTMusicAdapter(client_factory=lambda cred: fake)
    # Force small batches to exercise chunking.
    monkeypatch.setattr(adapter.info.capabilities, "max_add_batch", 2)

    pid = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="M"))
    uris = [f"yt:video:v{i}" for i in range(5)]
    results = await adapter.add_tracks(_cred(), pid, uris)

    assert calls == [2, 2, 1]
    assert [r.position for r in results] == [0, 1, 2, 3, 4]
    assert all(r.ok for r in results)


async def test_create_playlist_failure_raises() -> None:
    class Failing:
        def create_playlist(self, *a, **k):
            return {"error": "nope"}

        def add_playlist_items(self, *a, **k):
            return {"status": "STATUS_FAILED"}

    adapter = _adapter(Failing())
    with pytest.raises(ProviderError):
        await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="M"))


async def test_add_tracks_failure_reports_per_item() -> None:
    class Failing:
        def add_playlist_items(self, *a, **k):
            return {"status": "STATUS_FAILED", "error": "bad"}

    adapter = _adapter(Failing())
    results = await adapter.add_tracks(_cred(), "PL", ["yt:video:a", "yt:video:b"])
    assert [r.ok for r in results] == [False, False]
    assert all(r.error for r in results)


async def test_search_tracks_maps_song_results() -> None:
    adapter = _adapter(FakeYTMusic())
    results = await adapter.search_tracks(_cred(), Track(title="Song One", artist="Artist One"))
    assert len(results) == 1
    assert results[0].provider_track_id == "yt_song_one"
    assert results[0].uri == "ytmusic:video:yt_song_one"
    assert results[0].artist == "Artist One"
    assert results[0].explicit is False


async def test_header_auth_json_returns_credential() -> None:
    adapter = _adapter(FakeYTMusic())
    cred = await adapter.auth.complete(
        user_id="local",
        callback={"headers_raw": '{"Authorization":"Bearer x","Cookie":"SID=y"}'},
    )
    assert cred.auth_kind is AuthKind.HEADER_PASTE
    assert cred.extra["auth"]["Authorization"] == "Bearer x"
