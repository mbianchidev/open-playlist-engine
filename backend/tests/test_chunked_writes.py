from collections.abc import Sequence

from app.core.adapter import (
    AddItemResult,
    AuthKind,
    ProviderCredential,
    ProviderInfo,
    TrackCandidate,
)
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.match_service import MatchResult
from app.core.models import Track
from app.db import models as orm
from app.jobs import migration as migration_job


class FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)


class ChunkTarget:
    def __init__(self) -> None:
        self.info = ProviderInfo(
            name="target",
            display_name="Target",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={Capability.ADD_TRACKS},
                max_add_batch=2,
            ),
        )
        self.calls: list[list[str]] = []

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self.calls.append(list(uris))
        return [
            AddItemResult(uri=uri, ok=True, position=position)
            for position, uri in enumerate(uris)
        ]


def _item(uri: str) -> orm.JobItem:
    return orm.JobItem(
        job_id="job",
        source_playlist_id="source-playlist",
        position=0,
        title=uri,
        artist="Artist",
        target_playlist_id="target-playlist",
        target_uri=uri,
        source_metadata={},
        status="matched",
    )


async def test_flush_matched_chunk_waits_until_provider_batch_is_full(
    monkeypatch,
) -> None:
    async def noop_commit(session, job) -> None:
        return None

    monkeypatch.setattr(migration_job, "commit_job_counts", noop_commit)
    target = ChunkTarget()
    session = FakeSession()
    job = orm.MigrationJob(
        id="job",
        user_id="local",
        source_provider="source",
        target_provider="target",
    )
    cred = ProviderCredential(
        account_id="target-account",
        provider="target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )
    first = _item("target:one")
    matched = [first]
    existing_keys: set[str] = set()

    await migration_job._flush_matched_chunk(
        session,
        job,
        target,
        cred,
        "target-playlist",
        matched,
        existing_keys=existing_keys,
    )

    assert target.calls == []
    assert [item.target_uri for item in matched] == ["target:one"]

    second = _item("target:two")
    matched.append(second)
    await migration_job._flush_matched_chunk(
        session,
        job,
        target,
        cred,
        "target-playlist",
        matched,
        existing_keys=existing_keys,
    )

    assert target.calls == [["target:one", "target:two"]]
    assert matched == []
    assert first.status == "written"
    assert second.status == "written"
    assert len(session.added) == 2


def test_prior_accepted_review_match_replaces_candidate_and_boosts_confidence() -> None:
    prior = orm.JobItem(
        id="prior",
        job_id="old-job",
        source_playlist_id="playlist",
        position=0,
        title="Hard Song",
        artist="Artist",
        album="Album",
        duration_s=180,
        source_metadata={"title": "Hard Song", "artist": "Artist", "album": "Album"},
        target_uri="ytmusic:video:accepted",
        confidence=0.62,
        status="written",
    )
    current = MatchResult(
        candidate=TrackCandidate(
            provider_track_id="other",
            uri="ytmusic:video:other",
            title="Hard Song?",
            artist="Artist",
        ),
        confidence=0.58,
        source="fuzzy",
        needs_review=True,
    )

    result = migration_job._apply_review_history_bonus(
        [prior],
        track=Track(title="Hard Song", artist="Artist", album="Album", duration_s=180),
        result=current,
        review_threshold=0.8,
    )

    assert result.candidate is not None
    assert result.candidate.uri == "ytmusic:video:accepted"
    assert result.confidence == 0.72
    assert result.needs_review is True
    assert result.review_reason is not None


def test_prior_edited_review_match_can_clear_review_after_confidence_boost() -> None:
    prior = orm.JobItem(
        id="prior",
        job_id="old-job",
        source_playlist_id="playlist",
        position=0,
        title="Hard Song",
        artist="Artist",
        source_metadata={"title": "Hard Song", "artist": "Artist"},
        target_uri="ytmusic:video:edited",
        confidence=0.76,
        status="written",
    )
    current = MatchResult(
        candidate=None,
        confidence=0.0,
        source="none",
        needs_review=True,
    )

    result = migration_job._apply_review_history_bonus(
        [prior],
        track=Track(title="Hard Song", artist="Artist"),
        result=current,
        review_threshold=0.8,
    )

    assert result.candidate is not None
    assert result.candidate.uri == "ytmusic:video:edited"
    assert result.confidence == 0.86
    assert result.needs_review is False
