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


def test_target_only_live_version_gets_flat_confidence_penalty() -> None:
    track = Track(title="Song One", artist="Artist One", isrc="US0000000001", duration_s=180)
    candidate = TrackCandidate(
        provider_track_id="live",
        uri="ytmusic:video:live",
        title="Song One - Live",
        artist="Artist One",
        isrc="US0000000001",
        duration_s=180,
    )

    confidence, source = score(track, candidate)

    assert source == "isrc_exact_live"
    assert confidence == 0.8


def test_source_live_version_does_not_penalize_target_live_version() -> None:
    track = Track(title="Song One - Live", artist="Artist One", duration_s=180)
    candidate = TrackCandidate(
        provider_track_id="live",
        uri="ytmusic:video:live",
        title="Song One - Live",
        artist="Artist One",
        duration_s=180,
    )

    confidence, source = score(track, candidate)

    assert source == "fuzzy"
    assert confidence >= 0.9


class LiveOnlyAdapter(FakeAdapter):
    async def search_tracks(
        self, cred, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        return [
            TrackCandidate(
                provider_track_id="live",
                uri="ytmusic:video:live",
                title=f"{track.title} - Live",
                artist=track.artist,
                duration_s=track.duration_s,
            )
        ]


async def test_resolve_reviews_live_target_when_source_is_not_live() -> None:
    svc = MatchService(graph=None, review_threshold=0.8)
    track = Track(title="Song One", artist="Artist One", duration_s=180)

    res = await svc.resolve(track, LiveOnlyAdapter(), fake_cred("fake"))

    assert res.candidate is not None
    assert res.confidence == 0.5929
    assert res.source == "fuzzy_live"
    assert res.needs_review is True
    assert res.review_reason is not None
    assert "live version" in res.review_reason
