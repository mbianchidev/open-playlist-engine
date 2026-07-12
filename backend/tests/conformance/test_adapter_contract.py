"""The provider conformance suite.

Any adapter (real or fake) must satisfy these behaviours. The fake exercises the
whole contract; real adapters are parametrized against recorded fixtures (Spotify
and Tidal) or an injected in-memory client (YouTube Music) — never live
APIs in CI. See ``cases.py`` for what each adapter puts in scope.
"""

from __future__ import annotations

import pytest

from app.core.adapter import NotFound, ProviderAdapter
from app.core.models import Track
from tests.conformance.cases import Case, build_cases


@pytest.fixture(params=build_cases(), ids=lambda c: c.id)
def case(request: pytest.FixtureRequest) -> Case:
    return request.param


def test_satisfies_protocol(case: Case) -> None:
    assert isinstance(case.adapter, ProviderAdapter)


async def test_iter_and_read_roundtrip(case: Case) -> None:
    if not case.reads:
        pytest.skip(f"{case.id}: read not in scope")
    refs = [r async for r in case.adapter.iter_playlists(case.cred)]
    assert refs, "expected at least one playlist"
    pl = await case.adapter.read_playlist(case.cred, refs[0])
    assert pl.tracks
    if case.expect_isrc:
        # ISRC must survive the round-trip (it is the primary match key).
        assert all(t.isrc for t in pl.tracks)


async def test_read_missing_raises_notfound(case: Case) -> None:
    if not case.reads:
        pytest.skip(f"{case.id}: read not in scope")
    with pytest.raises(NotFound):
        await case.adapter.read_playlist(case.cred, case.missing_ref)


async def test_search_returns_candidates(case: Case) -> None:
    if not case.searches:
        pytest.skip(f"{case.id}: search not in scope")
    hits = await case.adapter.search_tracks(
        case.cred, Track(title=case.search_title, artist=case.search_artist)
    )
    assert hits and hits[0].uri.startswith(case.search_uri_prefix)


async def test_create_then_add_reports_per_item(case: Case) -> None:
    if not case.writes:
        pytest.skip(f"{case.id}: write not in scope")
    assert case.create_spec is not None
    pid = await case.adapter.create_playlist(case.cred, case.create_spec)
    results = await case.adapter.add_tracks(case.cred, pid, case.add_uris)
    assert [r.uri for r in results] == case.add_uris
    assert all(r.ok for r in results)
    assert [r.position for r in results] == list(range(len(case.add_uris)))


def test_add_respects_batch_limit_metadata(case: Case) -> None:
    if not case.writes:
        pytest.skip(f"{case.id}: write not in scope")
    # The capability descriptor must advertise a usable batch bound.
    assert case.adapter.info.capabilities.max_add_batch >= 1
