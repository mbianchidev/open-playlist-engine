from datetime import UTC, datetime

from app.api import migrations
from app.db import models as orm


class FakeSession:
    pass


async def test_review_skip_clears_target_uri_and_records_decision(monkeypatch) -> None:
    async def noop_commit(session, job) -> None:
        return None

    monkeypatch.setattr(migrations, "commit_job_counts", noop_commit)
    job = orm.MigrationJob(
        id="job",
        user_id="local",
        source_provider="spotify",
        target_provider="ytmusic",
    )
    item = orm.JobItem(
        id="item",
        job_id="job",
        source_playlist_id="playlist",
        position=0,
        title="Song",
        artist="Artist",
        target_playlist_id="target-playlist",
        target_uri="ytmusic:video:maybe",
        status="needs_review",
        reason="Low confidence",
    )
    reviewed_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(migrations, "_utcnow", lambda: reviewed_at)

    view = await migrations._apply_review(
        FakeSession(),
        job,
        item,
        migrations.ReviewItem(action="skip"),
    )

    assert view.status == "skipped"
    assert view.target_uri is None
    assert item.target_uri is None
    assert item.review_action == "skip"
    assert item.review_original_status == "needs_review"
    assert item.review_original_reason == "Low confidence"
    assert item.reviewed_at == reviewed_at
