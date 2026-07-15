from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import migrations, snapshots
from app.api.dependencies import get_current_user_id
from app.db import models as orm
from app.db.account_scope import migration_source_history
from app.db.base import Base
from app.jobs import migration as migration_job


def _job(
    job_id: str,
    *,
    source_kind: str,
    source_account_id: str,
    source_provider: str = "snapshot",
) -> orm.MigrationJob:
    return orm.MigrationJob(
        id=job_id,
        user_id="local",
        source_kind=source_kind,
        source_provider=source_provider,
        source_account_id=source_account_id,
        target_provider="ytmusic",
        target_account_id="target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
    )


def test_create_migration_requires_exactly_one_source_mode() -> None:
    with pytest.raises(ValidationError):
        migrations.CreateMigration(
            target_provider="ytmusic",
            target_account_id="target",
            selection={"playlist_ids": ["playlist"], "tracks": {}},
        )
    with pytest.raises(ValidationError):
        migrations.CreateMigration(
            source_provider="spotify",
            source_account_id="source",
            source_snapshot_id="snapshot",
            target_provider="ytmusic",
            target_account_id="target",
            selection={"playlist_ids": ["playlist"], "tracks": {}},
        )

    live = migrations.CreateMigration(
        source_provider="spotify",
        source_account_id="source",
        target_provider="ytmusic",
        target_account_id="target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
    )
    restore = migrations.CreateMigration(
        source_snapshot_id="snapshot",
        target_provider="ytmusic",
        target_account_id="target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
    )

    assert live.source_snapshot_id is None
    assert restore.source_provider is None
    assert restore.source_account_id is None


def test_snapshot_history_is_exactly_scoped_to_library_lineage() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _job(
                    "snapshot-a-prior",
                    source_kind="snapshot",
                    source_account_id="snapshot:library-a",
                ),
                _job(
                    "snapshot-b-prior",
                    source_kind="snapshot",
                    source_account_id="snapshot:library-b",
                ),
                _job(
                    "deleted-live-account",
                    source_kind="provider",
                    source_account_id="deleted-account",
                    source_provider="spotify",
                ),
            ]
        )
        session.commit()

        rows = session.scalars(
            select(orm.MigrationJob.id)
            .where(
                migration_source_history(
                    orm.MigrationJob.source_account_id,
                    orm.MigrationJob.source_kind,
                    current_account_id="snapshot:library-a",
                    current_source_kind="snapshot",
                    user_id="local",
                    provider="snapshot",
                )
            )
            .order_by(orm.MigrationJob.id)
        ).all()

    assert rows == ["snapshot-a-prior"]


def test_live_source_history_excludes_snapshot_jobs() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                orm.ProviderAccount(
                    id="current",
                    user_id="local",
                    provider="spotify",
                    provider_user_id="provider-user",
                ),
                _job(
                    "live",
                    source_kind="provider",
                    source_account_id="current",
                    source_provider="spotify",
                ),
                _job(
                    "snapshot",
                    source_kind="snapshot",
                    source_account_id="snapshot:library",
                    source_provider="spotify",
                ),
            ]
        )
        session.commit()

        rows = session.scalars(
            select(orm.MigrationJob.id)
            .where(
                migration_source_history(
                    orm.MigrationJob.source_account_id,
                    orm.MigrationJob.source_kind,
                    current_account_id="current",
                    current_source_kind="provider",
                    user_id="local",
                    provider="spotify",
                )
            )
            .order_by(orm.MigrationJob.id)
        ).all()

    assert rows == ["live"]


@pytest.mark.asyncio
async def test_snapshot_restore_rerun_skips_existing_target_track(monkeypatch) -> None:
    class SessionStub:
        async def flush(self) -> None:
            return None

    async def noop_commit(session, job) -> None:
        return None

    monkeypatch.setattr(migration_job, "commit_job_counts", noop_commit)
    job = _job(
        "restore",
        source_kind="snapshot",
        source_account_id="snapshot:library",
    )
    item = orm.JobItem(
        job_id=job.id,
        source_playlist_id="collection",
        position=0,
        title="Song",
        artist="Artist",
        isrc="US0000000001",
        target_uri="ytmusic:video:one",
        source_metadata={"isrc": "US0000000001"},
        status="matched",
    )

    pending = await migration_job._skip_duplicate_items(
        SessionStub(),
        job,
        [item],
        existing_keys={"isrc:US0000000001"},
    )

    assert pending == []
    assert item.status == "skipped"
    assert item.reason == "duplicate already exists in target playlist"


def test_all_snapshot_routes_require_current_user_dependency() -> None:
    snapshot_routes = [
        route
        for route in snapshots.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/snapshots")
    ]

    assert snapshot_routes
    for route in snapshot_routes:
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        assert get_current_user_id in dependencies, f"{route.methods} {route.path}"


@pytest.mark.asyncio
async def test_import_requires_explicit_confirmation_before_reading_request() -> None:
    class RequestStub:
        def stream(self):
            raise AssertionError("request body must not be read before confirmation")

    with pytest.raises(HTTPException) as exc_info:
        await snapshots.import_snapshot(
            RequestStub(),
            session=object(),
            user_id="local",
            confirm=False,
        )

    assert exc_info.value.status_code == 400
    assert "confirm=true" in exc_info.value.detail


def test_database_prevents_two_active_snapshots_for_one_profile() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            orm.SnapshotProfile(
                id="profile",
                user_id="local",
                name="Profile",
            )
        )
        session.add(
            orm.LibrarySnapshot(
                id="11111111-1111-4111-8111-111111111111",
                user_id="local",
                profile_id="profile",
                bundle_id="11111111-1111-4111-8111-111111111111",
                library_id="profile",
                source_providers=["spotify"],
                source_labels=[],
                status="pending",
                schema_version=1,
                size_bytes=0,
                manifest={},
                errors=[],
            )
        )
        session.commit()
        session.add(
            orm.LibrarySnapshot(
                id="22222222-2222-4222-8222-222222222222",
                user_id="local",
                profile_id="profile",
                bundle_id="22222222-2222-4222-8222-222222222222",
                library_id="profile",
                source_providers=["spotify"],
                source_labels=[],
                status="running",
                schema_version=1,
                size_bytes=0,
                manifest={},
                errors=[],
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
