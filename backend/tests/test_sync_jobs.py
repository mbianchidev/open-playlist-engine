from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.adapter import RateLimited, RefreshTokenExpired
from app.db import models as orm
from app.db.base import Base
from app.jobs.sync import (
    SyncAlreadyRunning,
    SyncPartialFailure,
    classify_job_items,
    create_queued_run,
    lease_is_current,
    recover_stale_runs,
    resolved_review_runs,
    review_finalization_ready,
    sync_error_policy,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _rule() -> orm.SyncRule:
    return orm.SyncRule(
        user_id="local",
        source_provider="source",
        source_account_id="source-account",
        source_playlist_id="source-playlist",
        source_playlist_name="Source",
        target_provider="target",
        target_account_id="target-account",
        target_playlist_id="target-playlist",
        target_playlist_name="Target",
        mode="add_only",
        cadence_minutes=60,
        timezone="UTC",
        enabled=True,
        status="idle",
    )


async def test_active_rule_rejects_overlapping_runs(session) -> None:
    rule = _rule()
    session.add(rule)
    await session.commit()
    now = datetime.now(UTC)

    await create_queued_run(session, rule_id=rule.id, trigger="manual", now=now)
    await session.commit()

    with pytest.raises(SyncAlreadyRunning):
        await create_queued_run(session, rule_id=rule.id, trigger="scheduled", now=now)


async def test_stale_recovery_invalidates_the_old_lease(session) -> None:
    rule = _rule()
    session.add(rule)
    await session.flush()
    run = orm.SyncRun(
        rule_id=rule.id,
        trigger="scheduled",
        status="running",
        lease_token="old-lease",
        queue_job_id="sync-run:stale",
        heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
    )
    session.add(run)
    await session.commit()
    now = datetime.now(UTC)

    recovered = await recover_stale_runs(
        session,
        now=now,
        stale_after_s=3600,
        retry_delay_s=300,
    )

    assert recovered == 1
    assert run.status == "failed"
    assert run.lease_token != "old-lease"
    assert rule.status == "failed"
    assert rule.next_run_at == now + timedelta(seconds=300)
    assert lease_is_current(run, "old-lease") is False


async def test_stale_review_finalizer_returns_to_review_queue(session) -> None:
    rule = _rule()
    rule.status = "running"
    session.add(rule)
    await session.flush()
    session.add(
        orm.SyncCheckpoint(
            rule_id=rule.id,
            source_snapshot={},
            target_snapshot={},
            mappings={},
            unresolved=["item"],
        )
    )
    run = orm.SyncRun(
        rule_id=rule.id,
        trigger="scheduled",
        status="running",
        lease_token="review-lease",
        queue_job_id="sync-run:review-stale",
        heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
    )
    session.add(run)
    await session.flush()
    job = orm.MigrationJob(
        user_id="local",
        source_provider="source",
        target_provider="target",
        source_account_id="source-account",
        target_account_id="target-account",
        selection={"playlist_ids": ["source-playlist"], "match_only": True},
        status="done",
        origin="sync",
        sync_run_id=run.id,
    )
    session.add(job)
    await session.flush()
    session.add(
        orm.JobItem(
            job_id=job.id,
            source_playlist_id="source-playlist",
            position=0,
            title="Resolved",
            artist="Artist",
            source_metadata={},
            target_uri="target:track:resolved",
            status="matched",
        )
    )
    await session.commit()

    recovered = await recover_stale_runs(
        session,
        now=datetime.now(UTC),
        stale_after_s=3600,
        retry_delay_s=300,
    )

    assert recovered == 1
    assert run.status == "review_required"
    assert rule.status == "review_required"
    assert rule.next_run_at is None


def test_sync_error_policy_auto_pauses_expired_credentials() -> None:
    policy = sync_error_policy(
        RefreshTokenExpired("spotify refresh token expired; reconnect Spotify")
    )

    assert policy.status == "reconnect_required"
    assert policy.pause_rule is True
    assert policy.retry_after_s is None


def test_sync_error_policy_respects_provider_retry_after() -> None:
    policy = sync_error_policy(RateLimited(retry_after_s=45))

    assert policy.status == "failed"
    assert policy.pause_rule is False
    assert policy.retry_after_s == 45


def test_sync_error_policy_preserves_partial_failure_status() -> None:
    policy = sync_error_policy(SyncPartialFailure("one write failed"))

    assert policy.status == "partial_failure"
    assert policy.pause_rule is False


def test_job_outcome_distinguishes_review_from_partial_write_failure() -> None:
    review = orm.JobItem(
        job_id="job",
        source_playlist_id="playlist",
        position=0,
        title="Missing",
        artist="Artist",
        source_metadata={},
        status="failed",
        reason="no target match found",
    )
    partial = orm.JobItem(
        job_id="job",
        source_playlist_id="playlist",
        position=1,
        title="Rejected",
        artist="Artist",
        source_metadata={},
        target_uri="target:track:rejected",
        status="failed",
        reason="target rejected track",
    )

    assert classify_job_items([review]).status == "review_required"
    assert classify_job_items([partial]).status == "partial_failure"
    assert review_finalization_ready([review]) is False

    review.status = "skipped"
    partial.status = "written"
    assert classify_job_items([review, partial]).status == "succeeded"
    assert review_finalization_ready([review, partial]) is True


async def test_resolved_review_runs_are_rediscovered(session) -> None:
    rule = _rule()
    rule.status = "review_required"
    session.add(rule)
    await session.flush()
    run = orm.SyncRun(
        rule_id=rule.id,
        trigger="scheduled",
        status="review_required",
        lease_token="review-lease",
        queue_job_id="sync-run:review",
    )
    session.add(run)
    await session.flush()
    job = orm.MigrationJob(
        user_id="local",
        source_provider="source",
        target_provider="target",
        source_account_id="source-account",
        target_account_id="target-account",
        selection={"playlist_ids": ["source-playlist"], "match_only": True},
        status="done",
        origin="sync",
        sync_run_id=run.id,
    )
    session.add(job)
    await session.flush()
    session.add(
        orm.JobItem(
            job_id=job.id,
            source_playlist_id="source-playlist",
            position=0,
            title="Resolved",
            artist="Artist",
            source_metadata={},
            target_uri="target:track:resolved",
            status="matched",
        )
    )
    await session.commit()

    assert [row.id for row in await resolved_review_runs(session)] == [run.id]
