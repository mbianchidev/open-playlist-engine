from datetime import UTC, datetime

from app.api import migrations
from app.core.models import MigrationEntityType
from app.db import models as orm
from tests.conformance.fake_provider import FakeAdapter, fake_cred


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


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


async def test_review_can_approve_album_without_playlist_target(monkeypatch) -> None:
    async def noop_commit(session, job) -> None:
        return None

    adapter = FakeAdapter()

    async def load_credential(*args, **kwargs):
        return fake_cred("fake"), object()

    monkeypatch.setattr(migrations, "commit_job_counts", noop_commit)
    monkeypatch.setattr(migrations, "get", lambda provider: adapter)
    monkeypatch.setattr(migrations, "load_fresh_credential", load_credential)
    job = orm.MigrationJob(
        id="job",
        user_id="local",
        source_provider="spotify",
        target_provider="fake",
        target_account_id="fake-account",
    )
    item = orm.JobItem(
        id="album-item",
        job_id="job",
        entity_type=MigrationEntityType.ALBUM,
        source_entity_id="album-source",
        source_entity_name="Album",
        position=0,
        title="Album",
        artist="Artist",
        target_entity_id="old-album",
        target_uri="fake:album:AbC123",
        status="needs_review",
    )
    session = FakeSession()

    view = await migrations._apply_review(
        session,
        job,
        item,
        migrations.ReviewItem(action="approve"),
    )

    assert view.status == "written"
    assert view.target_playlist_id is None
    assert view.target_entity_id == "AbC123"
    assert any(isinstance(row, orm.OperationLedger) for row in session.added)
    decisions = [row for row in session.added if isinstance(row, orm.ReviewDecision)]
    assert len(decisions) == 1
    assert decisions[0].entity_type == MigrationEntityType.ALBUM
    assert decisions[0].source_entity_id == "album-source"
    assert decisions[0].target_entity_id == "AbC123"
