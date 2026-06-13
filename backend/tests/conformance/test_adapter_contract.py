"""The provider conformance suite.

Any adapter (real or fake) must satisfy these behaviours. Real providers should
parametrize this against recorded fixtures — never live APIs in CI.
"""

from __future__ import annotations

import pytest

from app.core.adapter import CreatePlaylistSpec, NotFound, ProviderAdapter
from app.core.models import PlaylistRef
from tests.conformance.fake_provider import FakeAdapter, fake_cred


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


def test_satisfies_protocol(adapter: FakeAdapter) -> None:
    assert isinstance(adapter, ProviderAdapter)


async def test_iter_and_read_roundtrip(adapter: FakeAdapter) -> None:
    cred = fake_cred("fake")
    refs = [r async for r in adapter.iter_playlists(cred)]
    assert refs, "expected at least one playlist"
    pl = await adapter.read_playlist(cred, refs[0])
    assert pl.tracks
    # ISRC must survive the round-trip (it is the primary match key).
    assert all(t.isrc for t in pl.tracks)


async def test_read_missing_raises_notfound(adapter: FakeAdapter) -> None:
    with pytest.raises(NotFound):
        await adapter.read_playlist(fake_cred("fake"), PlaylistRef(id="nope", name="nope"))


async def test_search_returns_candidates(adapter: FakeAdapter) -> None:
    hits = await adapter.search_tracks(fake_cred("fake"), _track("Song One"))
    assert hits and hits[0].uri.startswith("fake:track:")


async def test_create_then_add_reports_per_item(adapter: FakeAdapter) -> None:
    cred = fake_cred("fake")
    pid = await adapter.create_playlist(cred, CreatePlaylistSpec(name="Mirror"))
    uris = ["fake:track:Song One", "fake:track:Song Two"]
    results = await adapter.add_tracks(cred, pid, uris)
    assert [r.uri for r in results] == uris
    assert all(r.ok for r in results)
    assert [r.position for r in results] == [0, 1]


async def test_add_respects_batch_limit_metadata(adapter: FakeAdapter) -> None:
    # The capability descriptor must advertise a usable batch bound.
    assert adapter.info.capabilities.max_add_batch >= 1


def _track(title: str):
    from app.core.models import Track

    return Track(title=title, artist="Artist One")
