"""YouTube Music adapter specifics beyond the generic contract: videoId parsing,
privacy mapping, batching across ``max_add_batch`` and failure reporting."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.adapter import AuthKind, CreatePlaylistSpec, ProviderCredential, ProviderError
from app.providers.ytmusic.adapter import YTMusicAdapter, _video_id
from tests.conformance.ytmusic_fakes import FakeYTMusic


def _cred() -> ProviderCredential:
    return ProviderCredential(
        account_id="acc",
        provider="ytmusic",
        auth_kind=AuthKind.OAUTH_DEVICE,
        access_token="token",
    )


def _adapter(client: Any) -> YTMusicAdapter:
    return YTMusicAdapter(client_factory=lambda cred: client)


def test_video_id_parsing() -> None:
    assert _video_id("https://music.youtube.com/watch?v=abc123") == "abc123"
    assert _video_id("https://www.youtube.com/watch?v=abc123&list=PL1") == "abc123"
    assert _video_id("ytmusic:video:abc123") == "abc123"
    assert _video_id("abc123") == "abc123"


async def test_create_playlist_maps_privacy() -> None:
    fake = FakeYTMusic()
    adapter = _adapter(fake)
    pid = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="Pub", public=True))
    assert fake.playlists[pid]["privacy"] == "PUBLIC"
    pid2 = await adapter.create_playlist(_cred(), CreatePlaylistSpec(name="Priv"))
    assert fake.playlists[pid2]["privacy"] == "PRIVATE"


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
