from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import migrations
from app.db import models as orm
from app.db.base import Base
from app.jobs import continuous_sync, history_cleanup


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _job(session) -> orm.MigrationJob:
    job = orm.MigrationJob(
        id="migration",
        user_id="local",
        source_provider="source",
        target_provider="target",
        source_account_id="source-account",
        target_account_id="target-account",
        status="done",
        origin="manual",
        selection={
            "playlist_ids": ["playlist"],
            "tracks": {},
            "continuous_sync": {
                "mode": "add_only",
                "cadence_minutes": 60,
                "timezone": "UTC",
            },
        },
    )
    session.add(job)
    await session.commit()
    return job


async def test_ensure_continuous_sync_persists_created_rule(session, monkeypatch) -> None:
    job = await _job(session)
    captured = {}

    async def create(body, *, session, user_id, allow_existing):
        captured["body"] = body
        captured["user_id"] = user_id
        captured["allow_existing"] = allow_existing
        return SimpleNamespace(id="sync-rule")

    monkeypatch.setattr(continuous_sync, "create_sync_rule", create)

    rule_id = await continuous_sync.ensure_continuous_sync(session, job)

    assert rule_id == "sync-rule"
    assert captured["body"].migration_job_id == "migration"
    assert captured["allow_existing"] is True
    assert job.selection["continuous_sync"]["status"] == "active"
    assert job.selection["continuous_sync"]["sync_rule_id"] == "sync-rule"
    assert job.selection["continuous_sync"]["error"] is None


async def test_ensure_continuous_sync_records_expected_setup_failure(session, monkeypatch) -> None:
    job = await _job(session)

    async def create(*args, **kwargs):
        raise HTTPException(status_code=409, detail="source playlist changed")

    monkeypatch.setattr(continuous_sync, "create_sync_rule", create)

    rule_id = await continuous_sync.ensure_continuous_sync(session, job)

    assert rule_id is None
    assert job.selection["continuous_sync"]["status"] == "failed"
    assert job.selection["continuous_sync"]["error"] == "source playlist changed"


async def test_ensure_continuous_sync_stays_pending_while_review_is_unresolved(
    session,
    monkeypatch,
) -> None:
    job = await _job(session)
    session.add(
        orm.JobItem(
            id="pending-review",
            job_id=job.id,
            source_playlist_id="playlist",
            position=0,
            title="One",
            artist="Artist",
            status="needs_review",
        )
    )
    await session.commit()

    async def create(*args, **kwargs):
        raise AssertionError("sync setup must wait for review")

    monkeypatch.setattr(continuous_sync, "create_sync_rule", create)

    rule_id = await continuous_sync.ensure_continuous_sync(session, job)

    assert rule_id is None
    assert "status" not in job.selection["continuous_sync"]


async def test_manual_review_creates_continuous_sync_only_after_all_items_resolve(
    session,
    monkeypatch,
) -> None:
    job = await _job(session)
    item = orm.JobItem(
        id="item",
        job_id=job.id,
        source_playlist_id="playlist",
        position=0,
        title="One",
        artist="Artist",
        status="needs_review",
    )
    session.add(item)
    await session.commit()
    calls = []

    async def ensure(session_arg, job_arg):
        calls.append(job_arg.id)
        return "sync-rule"

    monkeypatch.setattr(migrations, "ensure_continuous_sync", ensure)

    await migrations._maybe_finalize_sync_review(BackgroundTasks(), session, job)
    assert calls == []

    item.status = "written"
    await session.commit()
    await migrations._maybe_finalize_sync_review(BackgroundTasks(), session, job)

    assert calls == ["migration"]


def test_continuous_sync_requires_one_full_provider_playlist() -> None:
    with pytest.raises(ValidationError, match="full provider playlist"):
        migrations.CreateMigration(
            source_provider="source",
            source_account_id="source-account",
            target_provider="target",
            target_account_id="target-account",
            selection={
                "playlist_ids": ["playlist"],
                "tracks": {"playlist": ["track"]},
                "continuous_sync": {"mode": "add_only"},
            },
        )


def test_continuous_sync_rejects_invalid_schedule_before_migration() -> None:
    with pytest.raises(ValidationError, match="valid IANA"):
        migrations.ContinuousSyncIntent(timezone="Mars/Olympus")

    with pytest.raises(ValidationError, match="cadence_minutes"):
        migrations.ContinuousSyncIntent(cadence_minutes=1)


async def test_continuous_sync_preflight_rejects_inverse_existing_rule(session) -> None:
    session.add(
        orm.SyncRule(
            id="existing",
            user_id="local",
            source_provider="target",
            source_account_id="target-account",
            source_playlist_id="target-playlist",
            source_playlist_name="Target",
            target_provider="source",
            target_account_id="source-account",
            target_playlist_id="source-playlist",
            target_playlist_name="Source",
            mode="add_only",
            cadence_minutes=60,
            timezone="UTC",
        )
    )
    await session.commit()
    body = migrations.CreateMigration(
        source_provider="source",
        source_account_id="source-account",
        target_provider="target",
        target_account_id="target-account",
        selection={
            "playlist_ids": ["source-playlist"],
            "tracks": {},
            "continuous_sync": {"mode": "add_only"},
        },
    )

    with pytest.raises(HTTPException, match="feedback loop"):
        await migrations._ensure_no_continuous_sync_feedback(
            session,
            body,
            user_id="local",
        )


async def test_continuous_sync_preflight_rejects_inverse_pending_migration(session) -> None:
    session.add(
        orm.MigrationJob(
            id="pending-inverse",
            user_id="local",
            source_provider="target",
            target_provider="source",
            source_account_id="target-account",
            target_account_id="source-account",
            status="pending",
            origin="manual",
            selection={
                "playlist_ids": ["target-playlist"],
                "tracks": {},
                "continuous_sync": {"mode": "add_only"},
            },
        )
    )
    await session.commit()
    body = migrations.CreateMigration(
        source_provider="source",
        source_account_id="source-account",
        target_provider="target",
        target_account_id="target-account",
        selection={
            "playlist_ids": ["source-playlist"],
            "tracks": {},
            "continuous_sync": {"mode": "add_only"},
        },
    )

    with pytest.raises(HTTPException, match="feedback loop"):
        await migrations._ensure_no_continuous_sync_feedback(
            session,
            body,
            user_id="local",
        )


async def test_continuous_sync_preflight_rejects_duplicate_pending_migration(session) -> None:
    session.add(
        orm.MigrationJob(
            id="pending-same-direction",
            user_id="local",
            source_provider="source",
            target_provider="target",
            source_account_id="source-account",
            target_account_id="target-account",
            status="pending",
            origin="manual",
            selection={
                "playlist_ids": ["source-playlist"],
                "tracks": {},
                "continuous_sync": {"mode": "add_only"},
            },
        )
    )
    await session.commit()
    body = migrations.CreateMigration(
        source_provider="source",
        source_account_id="source-account",
        target_provider="target",
        target_account_id="target-account",
        selection={
            "playlist_ids": ["source-playlist"],
            "tracks": {},
            "continuous_sync": {"mode": "add_only"},
        },
    )

    with pytest.raises(HTTPException, match="already pending"):
        await migrations._ensure_no_continuous_sync_feedback(
            session,
            body,
            user_id="local",
        )


async def test_continuous_sync_preflight_rejects_multi_hop_pending_cycle(session) -> None:
    session.add_all(
        [
            orm.MigrationJob(
                id="pending-a-b",
                user_id="local",
                source_provider="a",
                target_provider="b",
                source_account_id="a-account",
                target_account_id="b-account",
                status="pending",
                origin="manual",
                selection={
                    "playlist_ids": ["a-playlist"],
                    "tracks": {},
                    "continuous_sync": {"mode": "add_only"},
                },
            ),
            orm.MigrationJob(
                id="pending-b-c",
                user_id="local",
                source_provider="b",
                target_provider="c",
                source_account_id="b-account",
                target_account_id="c-account",
                status="pending",
                origin="manual",
                selection={
                    "playlist_ids": ["b-playlist"],
                    "tracks": {},
                    "continuous_sync": {"mode": "add_only"},
                },
            ),
        ]
    )
    await session.commit()
    body = migrations.CreateMigration(
        source_provider="c",
        source_account_id="c-account",
        target_provider="a",
        target_account_id="a-account",
        selection={
            "playlist_ids": ["c-playlist"],
            "tracks": {},
            "continuous_sync": {"mode": "add_only"},
        },
    )

    with pytest.raises(HTTPException, match="feedback loop"):
        await migrations._ensure_no_continuous_sync_feedback(
            session,
            body,
            user_id="local",
        )


async def test_expired_continuous_sync_review_releases_pending_reservation(session) -> None:
    job = await _job(session)
    session.add(
        orm.JobItem(
            id="expiring-review",
            job_id=job.id,
            source_playlist_id="playlist",
            position=0,
            title="One",
            artist="Artist",
            status="needs_review",
        )
    )
    await session.commit()

    await history_cleanup._expire_continuous_sync_review(session, job)

    assert job.selection["continuous_sync"]["status"] == "failed"
    assert job.selection["continuous_sync"]["error"] == "migration review expired"
