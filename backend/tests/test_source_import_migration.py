"""Verify that public URL / pasted-text import records reuse the same
lease-backed migration lifecycle as local file imports (task #8: generalize
migrations API and worker checks from ``LOCAL_FILE_PROVIDER`` to all
``IMPORT_RECORD_PROVIDERS``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import migrations
from app.core.adapter import AuthKind, ProviderCredential
from app.core.models import Playlist, Track
from app.db import models as orm
from app.db.base import Base
from app.imports import PASTED_TEXT_PROVIDER, PUBLIC_URL_PROVIDER
from app.imports.external import ExternalImportResult
from app.imports.service import create_source_import, queue_import
from app.imports.source import open_migration_source
from app.jobs import migration as migration_job
from app.settings import Settings
from tests.test_local_import_migration import LocalImportTarget


@pytest.fixture
async def migration_database(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source_migration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
def migration_settings() -> Settings:
    return Settings(
        review_confidence_threshold=0.8,
        migration_safe_max_playlists_per_job=100,
        migration_safe_max_tracks_per_job=10_000,
        migration_safe_daily_tracks=10_000,
        migration_safe_min_job_gap_s=0,
        local_import_max_bytes=1_000_000,
        local_import_max_playlists=10,
        local_import_max_tracks=100,
        local_import_max_issues=20,
        local_import_retention_s=3_600,
        local_import_queued_retention_s=7_200,
        local_import_failed_retention_s=600,
    )


def _external_result(*, source_provider: str = "ytmusic") -> ExternalImportResult:
    return ExternalImportResult(
        source_provider=source_provider,
        source_label="Road Trip (shared)",
        source_locator="https://open.example/share/token123",
        source_fingerprint="fingerprint-1",
        playlist=Playlist(
            id="shared:playlist",
            name="Road Trip",
            tracks=[
                Track(
                    id="one",
                    title="Deja Vu",
                    artist="Beyonce",
                    source_item_id="shared:1",
                ),
                Track(
                    id="two",
                    title="One More Time",
                    artist="Daft Punk",
                    source_item_id="shared:2",
                ),
            ],
        ),
    )


async def test_open_migration_source_never_uses_provider_registry_for_public_url(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with migration_database() as session:
        record = await create_source_import(
            session,
            user_id="local",
            source_kind=PUBLIC_URL_PROVIDER,
            result=_external_result(),
            settings=migration_settings,
        )
        await session.commit()

        def unexpected_registry_lookup(name: str):
            raise AssertionError(f"provider registry used for import record: {name}")

        async def unexpected_credential(*args, **kwargs):
            raise AssertionError("credential repository used for import record")

        monkeypatch.setattr("app.imports.source.get", unexpected_registry_lookup)
        monkeypatch.setattr(
            "app.imports.source.load_fresh_credential",
            unexpected_credential,
        )

        source = await open_migration_source(
            session,
            provider=PUBLIC_URL_PROVIDER,
            account_id=record.id,
            user_id="local",
            settings=migration_settings,
        )
        playlist = await source.read_playlist(record.playlists[0]["id"])

    assert source.display_name == "Road Trip (shared)"
    assert playlist.name == "Road Trip"
    assert playlist.tracks[0].title == "Deja Vu"


async def test_create_migration_leases_public_url_import_once(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with migration_database() as session:
        record = await create_source_import(
            session,
            user_id="local",
            source_kind=PUBLIC_URL_PROVIDER,
            result=_external_result(),
            settings=migration_settings,
        )
        await session.commit()
        playlist_id = record.playlists[0]["id"]
        body = migrations.CreateMigration(
            source_provider=PUBLIC_URL_PROVIDER,
            target_provider="target",
            source_account_id=record.id,
            target_account_id="target-account",
            selection=migrations.Selection(playlist_ids=[playlist_id]),
            acknowledge_warnings=True,
        )

        async def no_warnings(*args, **kwargs):
            return migrations._ValidatedPreflight(
                source=migrations.MigrationSource(
                    kind="import",
                    provider=PUBLIC_URL_PROVIDER,
                    account_id=record.id,
                    display_name=record.source_label or "Public playlist",
                ),
                warnings=[],
                summary=migrations.MigrationSelectionSummary(playlists=1, tracks=2),
            )

        async def no_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(migrations, "_validated_preflight", no_warnings)
        monkeypatch.setattr(migrations, "_enqueue_or_inline", no_enqueue)
        monkeypatch.setattr(migrations, "get_settings", lambda: migration_settings)

        view = await migrations.create_migration(
            body,
            BackgroundTasks(),
            session,
            user_id="local",
        )
        leased = await session.get(orm.LocalPlaylistImport, record.id)
        assert leased is not None
        assert leased.status == "queued"
        assert leased.queued_job_id == view.id


async def test_worker_consumes_pasted_text_import_and_deletes_record(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = LocalImportTarget()
    async with migration_database() as session:
        record = await create_source_import(
            session,
            user_id="local",
            source_kind=PASTED_TEXT_PROVIDER,
            result=_external_result(source_provider=PASTED_TEXT_PROVIDER),
            settings=migration_settings,
        )
        job = orm.MigrationJob(
            id="source-job",
            user_id="local",
            source_provider=PASTED_TEXT_PROVIDER,
            target_provider="target",
            source_account_id=record.id,
            target_account_id="target-account",
            selection={"playlist_ids": [record.playlists[0]["id"]], "tracks": {}},
            status="pending",
        )
        session.add(job)
        await session.flush()
        await queue_import(
            session,
            import_id=record.id,
            user_id="local",
            job_id=job.id,
            settings=migration_settings,
        )
        await session.commit()
        import_id = record.id

    monkeypatch.setattr(migration_job, "get", lambda name: target)
    monkeypatch.setattr(migration_job, "get_settings", lambda: migration_settings)
    monkeypatch.setattr(migration_job, "get_sessionmaker", lambda: migration_database)
    monkeypatch.setattr(
        migration_job,
        "load_fresh_credential",
        lambda *args, **kwargs: _async_value((_target_credential(), None)),
    )

    await migration_job.run_migration({}, "source-job")

    async with migration_database() as session:
        job = await session.get(orm.MigrationJob, "source-job")
        import_record = await session.get(orm.LocalPlaylistImport, import_id)

    assert job is not None
    assert job.status == "done"
    assert import_record is None
    assert list(target.created.values()) == [
        ["target:track:Deja Vu", "target:track:One More Time"]
    ]


def _target_credential() -> ProviderCredential:
    return ProviderCredential(
        account_id="target-account",
        provider="target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )


async def _async_value(value):
    return value
