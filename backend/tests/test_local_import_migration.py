from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import migrations
from app.core.adapter import (
    AddItemResult,
    AuthKind,
    CreatePlaylistSpec,
    ProviderCredential,
    ProviderInfo,
    TrackCandidate,
)
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import Playlist, PlaylistRef, Track
from app.db import models as orm
from app.db.base import Base
from app.imports import LOCAL_FILE_PROVIDER
from app.imports.models import ImportLimits
from app.imports.registry import parse_playlist_file
from app.imports.service import create_import, queue_import
from app.imports.source import open_migration_source
from app.jobs import migration as migration_job
from app.settings import Settings


class LocalImportTarget:
    def __init__(self, *, fail_search: bool = False) -> None:
        self.info = ProviderInfo(
            name="target",
            display_name="Target",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={Capability.CREATE_PLAYLIST, Capability.ADD_TRACKS},
                max_add_batch=100,
            ),
        )
        self.fail_search = fail_search
        self.created: dict[str, list[str]] = {}

    async def iter_playlists(
        self, cred: ProviderCredential
    ) -> AsyncIterator[PlaylistRef]:
        for playlist_id, uris in self.created.items():
            yield PlaylistRef(id=playlist_id, name="Road Trip", track_count=len(uris))

    async def read_playlist(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> Playlist:
        return Playlist(
            id=ref.id,
            name=ref.name,
            tracks=[
                Track(
                    title=uri.rsplit(":", 1)[-1],
                    artist="Target Artist",
                    provider_uris={"target": uri},
                )
                for uri in self.created.get(ref.id, [])
            ],
        )

    async def search_tracks(
        self,
        cred: ProviderCredential,
        track: Track,
        *,
        limit: int = 5,
    ) -> list[TrackCandidate]:
        if self.fail_search:
            raise RuntimeError("target search exploded")
        return [
            TrackCandidate(
                provider_track_id=track.title,
                uri=f"target:track:{track.title}",
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration_s=track.duration_s,
                isrc=track.isrc,
            )
        ][:limit]

    async def create_playlist(
        self,
        cred: ProviderCredential,
        spec: CreatePlaylistSpec,
    ) -> str:
        playlist_id = f"target-{len(self.created) + 1}"
        self.created[playlist_id] = []
        return playlist_id

    async def add_tracks(
        self,
        cred: ProviderCredential,
        playlist_id: str,
        uris: Sequence[str],
    ) -> list[AddItemResult]:
        results = []
        for uri in uris:
            self.created[playlist_id].append(uri)
            results.append(
                AddItemResult(
                    uri=uri,
                    ok=True,
                    position=len(self.created[playlist_id]) - 1,
                )
            )
        return results


@pytest.fixture
async def migration_database(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
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


def _parse(payload: bytes, filename: str = "playlist.txt"):
    return parse_playlist_file(
        BytesIO(payload),
        filename=filename,
        limits=ImportLimits(max_upload_bytes=1_000_000, max_tracks=100),
    )


def _target_credential() -> ProviderCredential:
    return ProviderCredential(
        account_id="target-account",
        provider="target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )


async def test_local_source_loader_never_uses_provider_registry_or_credentials(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with migration_database() as session:
        record = await create_import(
            session,
            user_id="local",
            filename="playlist.txt",
            result=_parse(b"Beyonce - Deja Vu"),
            settings=migration_settings,
        )
        await session.commit()

        def unexpected_registry_lookup(name: str):
            raise AssertionError(f"provider registry used for local import: {name}")

        async def unexpected_credential(*args, **kwargs):
            raise AssertionError("credential repository used for local import")

        monkeypatch.setattr("app.imports.source.get", unexpected_registry_lookup)
        monkeypatch.setattr(
            "app.imports.source.load_fresh_credential",
            unexpected_credential,
        )

        source = await open_migration_source(
            session,
            provider=LOCAL_FILE_PROVIDER,
            account_id=record.id,
            user_id="local",
            settings=migration_settings,
        )
        playlist = await source.read_playlist(record.playlists[0]["id"])

    assert source.display_name == "Local file"
    assert playlist.name == "playlist"
    assert playlist.tracks[0].title == "Deja Vu"


async def test_local_preflight_surfaces_unsupported_selected_items(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = LocalImportTarget()
    async with migration_database() as session:
        record = await create_import(
            session,
            user_id="local",
            filename="playlist.txt",
            result=_parse(
                b"#PLAYLIST:Road Trip\nBeyonce - Deja Vu\n/Users/me/Music/local.mp3\n"
            ),
            settings=migration_settings,
        )
        await session.commit()
        playlist_id = record.playlists[0]["id"]
        body = migrations.CreateMigration(
            source_provider=LOCAL_FILE_PROVIDER,
            target_provider="target",
            source_account_id=record.id,
            target_account_id="target-account",
            selection=migrations.Selection(playlist_ids=[playlist_id]),
        )

        monkeypatch.setattr(migrations, "get", lambda name: target)
        monkeypatch.setattr(
            migrations,
            "load_fresh_credential",
            lambda *args, **kwargs: _async_value((_target_credential(), None)),
        )
        monkeypatch.setattr(migrations, "get_settings", lambda: migration_settings)

        warnings = await migrations._validated_preflight_warnings(
            session,
            body,
            user_id="local",
        )

    assert any(warning["code"] == "unsupported_items" for warning in warnings)


async def test_create_migration_leases_local_import_once(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with migration_database() as session:
        record = await create_import(
            session,
            user_id="local",
            filename="playlist.txt",
            result=_parse(b"Beyonce - Deja Vu"),
            settings=migration_settings,
        )
        await session.commit()
        playlist_id = record.playlists[0]["id"]
        body = migrations.CreateMigration(
            source_provider=LOCAL_FILE_PROVIDER,
            target_provider="target",
            source_account_id=record.id,
            target_account_id="target-account",
            selection=migrations.Selection(playlist_ids=[playlist_id]),
            acknowledge_warnings=True,
        )

        async def no_warnings(*args, **kwargs):
            return []

        async def no_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(migrations, "_validated_preflight_warnings", no_warnings)
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

        with pytest.raises(HTTPException) as exc_info:
            await migrations.create_migration(
                body,
                BackgroundTasks(),
                session,
                user_id="local",
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "import_queued"


async def test_creating_items_is_idempotent_for_worker_retry(
    migration_database,
) -> None:
    tracks = [
        Track(title="One", artist="Artist", position=0),
        Track(title="Two", artist="Artist", position=1),
    ]
    async with migration_database() as session:
        job = orm.MigrationJob(
            id="retry-job",
            user_id="local",
            source_provider=LOCAL_FILE_PROVIDER,
            target_provider="target",
            source_account_id="import",
            target_account_id="target",
            selection={},
            status="running",
        )
        session.add(job)
        await session.flush()
        first = await migration_job._create_items(
            session,
            job,
            "playlist",
            "Playlist",
            tracks,
        )
        first[0][0].status = "written"
        await session.commit()

        second = await migration_job._create_items(
            session,
            job,
            "playlist",
            "Playlist",
            tracks,
        )
        await session.commit()
        rows = list(
            (
                await session.execute(
                    select(orm.JobItem)
                    .where(orm.JobItem.job_id == job.id)
                    .order_by(orm.JobItem.position)
                )
            ).scalars()
        )

    assert len(rows) == 2
    assert [item.id for item, _ in second] == [row.id for row in rows]
    assert rows[0].status == "written"


async def test_worker_consumes_local_import_and_writes_supported_tracks(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = LocalImportTarget()
    async with migration_database() as session:
        record = await create_import(
            session,
            user_id="local",
            filename="playlist.txt",
            result=_parse(
                b"#PLAYLIST:Road Trip\n"
                b"Beyonce - Deja Vu\n"
                b"Daft Punk - One More Time\n"
                b"/Users/me/Music/local.mp3\n"
            ),
            settings=migration_settings,
        )
        job = orm.MigrationJob(
            id="job",
            user_id="local",
            source_provider=LOCAL_FILE_PROVIDER,
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

    _patch_worker(monkeypatch, migration_database, migration_settings, target)
    await migration_job.run_migration({}, "job")

    async with migration_database() as session:
        job = await session.get(orm.MigrationJob, "job")
        items = list(
            (
                await session.execute(
                    select(orm.JobItem)
                    .where(orm.JobItem.job_id == "job")
                    .order_by(orm.JobItem.position)
                )
            ).scalars()
        )
        import_record = await session.get(orm.LocalPlaylistImport, import_id)

    assert job is not None
    assert job.status == "done"
    assert [item.status for item in items] == ["written", "written", "skipped"]
    assert import_record is None
    assert list(target.created.values()) == [
        ["target:track:Deja Vu", "target:track:One More Time"]
    ]


async def test_failed_worker_keeps_import_for_retry_grace(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = LocalImportTarget(fail_search=True)
    async with migration_database() as session:
        record = await create_import(
            session,
            user_id="local",
            filename="playlist.txt",
            result=_parse(b"Beyonce - Deja Vu"),
            settings=migration_settings,
        )
        job = orm.MigrationJob(
            id="failed-job",
            user_id="local",
            source_provider=LOCAL_FILE_PROVIDER,
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

    _patch_worker(monkeypatch, migration_database, migration_settings, target)
    before = datetime.now(UTC)
    await migration_job.run_migration({}, "failed-job")

    async with migration_database() as session:
        job = await session.get(orm.MigrationJob, "failed-job")
        import_record = await session.get(orm.LocalPlaylistImport, import_id)

    assert job is not None
    assert job.status == "failed"
    assert import_record is not None
    assert import_record.status == "failed"
    expires_at = import_record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > before


async def test_worker_rejects_and_deletes_expired_queued_import(
    migration_database,
    migration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with migration_database() as session:
        record = await create_import(
            session,
            user_id="local",
            filename="playlist.txt",
            result=_parse(b"Beyonce - Deja Vu"),
            settings=migration_settings,
        )
        job = orm.MigrationJob(
            id="expired-job",
            user_id="local",
            source_provider=LOCAL_FILE_PROVIDER,
            target_provider="target",
            source_account_id=record.id,
            target_account_id="target-account",
            selection={"playlist_ids": [record.playlists[0]["id"]], "tracks": {}},
            status="pending",
        )
        session.add(job)
        await session.flush()
        record.status = "queued"
        record.queued_job_id = job.id
        record.expires_at = datetime.now(UTC)
        await session.commit()
        import_id = record.id

    _patch_worker(
        monkeypatch,
        migration_database,
        migration_settings,
        LocalImportTarget(),
    )
    await migration_job.run_migration({}, "expired-job")

    async with migration_database() as session:
        job = await session.get(orm.MigrationJob, "expired-job")
        import_record = await session.get(orm.LocalPlaylistImport, import_id)

    assert job is not None
    assert job.status == "failed"
    assert "expired before" in (job.error or "")
    assert import_record is None


def _patch_worker(
    monkeypatch: pytest.MonkeyPatch,
    sessionmaker,
    settings: Settings,
    target: LocalImportTarget,
) -> None:
    monkeypatch.setattr(migration_job, "get", lambda name: target)
    monkeypatch.setattr(migration_job, "get_settings", lambda: settings)
    monkeypatch.setattr(migration_job, "get_sessionmaker", lambda: sessionmaker)
    monkeypatch.setattr(
        migration_job,
        "load_fresh_credential",
        lambda *args, **kwargs: _async_value((_target_credential(), None)),
    )


async def _async_value(value):
    return value
