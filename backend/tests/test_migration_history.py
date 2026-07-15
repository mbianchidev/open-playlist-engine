from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.api import migrations
from app.db import models as orm
from app.db.base import Base
from app.db.migration_history import (
    MigrationItemFilters,
    details_available,
    effective_details_expires_at,
    mark_job_started,
    mark_job_terminal,
    migration_item_count_stmt,
    migration_items_stmt,
    migration_outcome,
)
from app.jobs import migration as migration_job


def _job(job_id: str, *, user_id: str = "alice") -> orm.MigrationJob:
    return orm.MigrationJob(
        id=job_id,
        user_id=user_id,
        source_provider="spotify",
        target_provider="ytmusic",
        source_account_id=f"{user_id}-source",
        target_account_id=f"{user_id}-target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
        status="done",
    )


def _item(
    item_id: str,
    job_id: str,
    *,
    title: str,
    reason: str,
    status: str = "failed",
    confidence: float | None = 0.42,
) -> orm.JobItem:
    return orm.JobItem(
        id=item_id,
        job_id=job_id,
        source_playlist_id="playlist",
        source_playlist_name="Road Trip",
        target_playlist_id="target-playlist",
        position=0,
        title=title,
        artist="The Artist",
        confidence=confidence,
        status=status,
        reason=reason,
    )


def test_item_filters_are_owner_scoped_and_escape_literal_wildcards() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    filters = MigrationItemFilters(
        source_playlist_id="playlist",
        statuses=("failed",),
        min_confidence=0.4,
        max_confidence=0.5,
        reason="100%",
        title="100%",
        artist="artist",
        problem_only=True,
    )
    with Session(engine) as session:
        session.add_all(
            [
                _job("alice-job"),
                _item(
                    "match",
                    "alice-job",
                    title="100% Pure",
                    reason="No 100% target match",
                ),
                _item(
                    "wildcard-decoy",
                    "alice-job",
                    title="1000 Pure",
                    reason="No 1000 target match",
                ),
                _job("bob-job", user_id="bob"),
                _item(
                    "other-user",
                    "bob-job",
                    title="100% Pure",
                    reason="No 100% target match",
                ),
            ]
        )
        session.commit()

        rows = session.scalars(
            migration_items_stmt(job_id="alice-job", user_id="alice", filters=filters)
        ).all()
        count = session.scalar(
            migration_item_count_stmt(job_id="alice-job", user_id="alice", filters=filters)
        )
        hidden = session.scalars(
            migration_items_stmt(job_id="alice-job", user_id="bob", filters=filters)
        ).all()

    assert [row.id for row in rows] == ["match"]
    assert count == 1
    assert hidden == []


def test_migration_outcome_distinguishes_completed_partial_and_failed() -> None:
    assert migration_outcome("pending", {"failed": 0}) == "pending"
    assert migration_outcome("running", {"failed": 0}) == "running"
    assert migration_outcome("failed", {"written": 4}) == "failed"
    assert migration_outcome("done", {"written": 4}) == "completed"
    assert migration_outcome("done", {"written": 3, "failed": 1}) == "partial"
    assert migration_outcome("done", {"written": 3, "needs_review": 1}) == "partial"
    assert migration_outcome("done", {"written": 3, "skipped": 1}) == "partial"


def test_lifecycle_helpers_set_duration_and_retention_deadline() -> None:
    started_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(minutes=7)
    job = _job("job")
    job.status = "pending"

    mark_job_started(job, now=started_at)
    mark_job_terminal(job, status="done", retention_days=90, now=completed_at)

    assert job.status == "done"
    assert job.started_at == started_at
    assert job.completed_at == completed_at
    assert job.details_expires_at == completed_at + timedelta(days=90)
    assert effective_details_expires_at(job, retention_days=90) == job.details_expires_at
    assert details_available(
        job,
        retention_days=90,
        now=job.details_expires_at - timedelta(seconds=1),
    )
    assert not details_available(job, retention_days=90, now=job.details_expires_at)


def test_zero_day_retention_keeps_detail_indefinitely() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    job = _job("job")
    job.completed_at = now

    assert effective_details_expires_at(job, retention_days=0) is None
    assert details_available(job, retention_days=0, now=now + timedelta(days=10_000))


def test_review_decisions_survive_item_and_operation_retention_cleanup() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _job("job"),
                _item("item", "job", title="Song", reason="Low confidence", status="written"),
                orm.OperationLedger(
                    id="ledger",
                    job_id="job",
                    op="review_add_track",
                    intent={"uri": "ytmusic:video:target"},
                    state="done",
                ),
                orm.ReviewDecision(
                    id="decision",
                    job_id="job",
                    user_id="alice",
                    source_provider="spotify",
                    target_provider="ytmusic",
                    source_account_id="alice-source",
                    target_account_id="alice-target",
                    title="Song",
                    artist="The Artist",
                    source_metadata={"id": "source-track"},
                    target_uri="ytmusic:video:target",
                    confidence=0.42,
                    status="written",
                    action="approve",
                ),
            ]
        )
        session.commit()

        session.execute(delete(orm.OperationLedger).where(orm.OperationLedger.job_id == "job"))
        session.execute(delete(orm.JobItem).where(orm.JobItem.job_id == "job"))
        session.commit()

        decision = session.get(orm.ReviewDecision, "decision")

    assert decision is not None
    assert decision.target_uri == "ytmusic:video:target"


def test_purged_summary_preserves_previous_target_playlist_reuse() -> None:
    prior = _job("prior")
    prior.result_summary = {
        "counts": {"total": 1, "written": 1},
        "playlists": [
            {
                "source_playlist_id": "playlist",
                "source_playlist_name": "Road Trip",
                "target_playlist_id": "remembered-target",
                "counts": {"total": 1, "written": 1},
            }
        ],
    }

    assert (
        migration_job._target_playlist_id_from_summaries([prior], "playlist")
        == "remembered-target"
    )
    assert migration_job._target_playlist_id_from_summaries([prior], "other") is None


class _OwnedJobSession:
    def __init__(self, job: orm.MigrationJob | None) -> None:
        self.job = job

    async def scalar(self, statement):
        return self.job


async def test_item_history_returns_explicit_not_found_for_another_users_job() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await migrations.get_migration_items(
            "private-job",
            Response(),
            _OwnedJobSession(None),
            "bob",
            source_playlist_id=None,
            statuses=None,
            min_confidence=None,
            max_confidence=None,
            reason=None,
            title=None,
            artist=None,
            problem_only=False,
            limit=None,
            offset=0,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "migration job not found"


async def test_item_history_returns_gone_after_retention_expiry() -> None:
    expired = datetime.now(UTC) - timedelta(seconds=1)
    job = _job("expired")
    job.details_expires_at = expired

    with pytest.raises(HTTPException) as exc_info:
        await migrations.get_migration_items(
            job.id,
            Response(),
            _OwnedJobSession(job),
            "alice",
            source_playlist_id=None,
            statuses=None,
            min_confidence=None,
            max_confidence=None,
            reason=None,
            title=None,
            artist=None,
            problem_only=False,
            limit=None,
            offset=0,
        )

    assert exc_info.value.status_code == 410
    assert "expired at" in str(exc_info.value.detail)


def test_account_history_never_uses_another_users_account_label() -> None:
    other_users_account = orm.ProviderAccount(
        id="shared-id",
        user_id="bob",
        provider="spotify",
        display_name="Bob's account",
    )

    view = migrations._account_history_view("shared-id", "spotify", {}, user_id="alice")
    owned_view = migrations._account_history_view(
        "shared-id",
        "spotify",
        {"shared-id": other_users_account},
        user_id="alice",
    )

    assert view == migrations.AccountHistoryView(
        id="shared-id", display_name=None, connected=False
    )
    assert owned_view == migrations.AccountHistoryView(
        id="shared-id", display_name=None, connected=False
    )
