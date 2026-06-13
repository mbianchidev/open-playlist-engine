from __future__ import annotations

from app.core.adapter import TrackCandidate
from app.core.match_service import MatchService, score
from app.core.models import Track
from tests.conformance.fake_provider import FakeAdapter, fake_cred


def test_isrc_exact_is_one() -> None:
    t = Track(title="A", artist="B", isrc="USAB12345678")
    c = TrackCandidate(
        provider_track_id="x", uri="x", title="totally different", artist="z", isrc="USAB12345678"
    )
    conf, src = score(t, c)
    assert conf == 1.0
    assert src == "isrc_exact"


def test_fuzzy_title_artist() -> None:
    t = Track(title="Bohemian Rhapsody", artist="Queen", duration_s=355)
    c = TrackCandidate(
        provider_track_id="x", uri="x", title="Bohemian Rhapsody", artist="Queen", duration_s=356
    )
    conf, src = score(t, c)
    assert src == "fuzzy"
    assert conf >= 0.9


async def test_resolve_picks_best_candidate() -> None:
    svc = MatchService(graph=None, review_threshold=0.8)
    target = FakeAdapter()
    track = Track(title="Song One", artist="Artist One")
    res = await svc.resolve(track, target, fake_cred("fake"))
    assert res.candidate is not None
    assert res.needs_review is False


async def test_resolve_needs_review_when_no_candidate() -> None:
    svc = MatchService(graph=None, review_threshold=0.8)
    target = FakeAdapter()
    track = Track(title="Nonexistent Zzz", artist="Nobody")
    res = await svc.resolve(track, target, fake_cred("fake"))
    assert res.candidate is None
    assert res.needs_review is True
