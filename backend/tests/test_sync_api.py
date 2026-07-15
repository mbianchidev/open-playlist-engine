import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import syncs
from app.core.adapter import AuthKind, ProviderCredential, ProviderInfo
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import Playlist, PlaylistRef, Track
from app.db import models as orm
from app.db.base import Base


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class _Adapter:
    auth = object()

    def __init__(self, name: str, playlists: dict[str, Playlist]) -> None:
        capabilities = {
            Capability.READ_PLAYLISTS,
            Capability.READ_TRACKS,
            Capability.CREATE_PLAYLIST,
            Capability.ADD_TRACKS,
        }
        self.info = ProviderInfo(
            name=name,
            display_name=name.title(),
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(capabilities=capabilities),
        )
        self.playlists = playlists

    async def read_playlist(self, cred, ref: PlaylistRef) -> Playlist:
        return self.playlists[ref.id]


class _MirrorAdapter(_Adapter):
    def __init__(self, name: str, playlists: dict[str, Playlist]) -> None:
        super().__init__(name, playlists)
        self.info = self.info.model_copy(
            update={
                "capabilities": self.info.capabilities.model_copy(
                    update={
                        "capabilities": self.info.capabilities.capabilities
                        | {Capability.REMOVE_TRACKS, Capability.REORDER}
                    }
                )
            }
        )

    async def replace_playlist_tracks(self, cred, playlist_id, uris) -> None:
        return None


async def _completed_migration(session) -> orm.MigrationJob:
    job = orm.MigrationJob(
        id="manual-job",
        user_id="local",
        source_provider="source",
        target_provider="target",
        source_account_id="source-account",
        target_account_id="target-account",
        selection={"playlist_ids": ["source-playlist"], "tracks": {}},
        status="done",
        origin="manual",
    )
    session.add(job)
    session.add_all(
        [
            orm.JobItem(
                job_id=job.id,
                source_playlist_id="source-playlist",
                source_playlist_name="Source Playlist",
                target_playlist_id="target-playlist",
                position=0,
                title="One",
                artist="Artist",
                source_metadata=Track(
                    id="one",
                    title="One",
                    artist="Artist",
                    position=0,
                    provider_uris={"source": "source:track:one"},
                ).model_dump(mode="json"),
                target_uri="target:track:one",
                status="written",
            ),
            orm.JobItem(
                job_id=job.id,
                source_playlist_id="source-playlist",
                source_playlist_name="Source Playlist",
                target_playlist_id="target-playlist",
                position=1,
                title="Two",
                artist="Artist",
                source_metadata=Track(
                    id="two",
                    title="Two",
                    artist="Artist",
                    position=1,
                    provider_uris={"source": "source:track:two"},
                ).model_dump(mode="json"),
                target_uri="target:track:two",
                status="skipped",
                reason="duplicate already exists in target playlist",
            ),
        ]
    )
    await session.commit()
    return job


def _adapters(*, mirror: bool = False) -> dict[str, _Adapter]:
    source = Playlist(
        id="source-playlist",
        name="Source Playlist",
        tracks=[
            Track(
                id="one",
                title="One",
                artist="Artist",
                position=0,
                provider_uris={"source": "source:track:one"},
            ),
            Track(
                id="two",
                title="Two",
                artist="Artist",
                position=1,
                provider_uris={"source": "source:track:two"},
            ),
        ],
    )
    target = Playlist(
        id="target-playlist",
        name="Target Playlist",
        tracks=[
            Track(
                id="one",
                title="One",
                artist="Artist",
                position=0,
                provider_uris={"target": "target:track:one"},
            ),
            Track(
                id="two",
                title="Two",
                artist="Artist",
                position=1,
                provider_uris={"target": "target:track:two"},
            ),
        ],
    )
    target_adapter = _MirrorAdapter if mirror else _Adapter
    return {
        "source": _Adapter("source", {source.id or "": source}),
        "target": target_adapter("target", {target.id or "": target}),
    }


async def _fake_credential(*args, account_id: str, provider: str, **kwargs):
    return (
        ProviderCredential(
            account_id=account_id,
            provider=provider,
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
        ),
        None,
    )


async def test_create_sync_from_completed_migration_persists_initial_checkpoint(
    session,
    monkeypatch,
) -> None:
    await _completed_migration(session)
    adapters = _adapters()
    monkeypatch.setattr(syncs, "get", adapters.__getitem__)
    monkeypatch.setattr(syncs, "load_fresh_credential", _fake_credential)

    view = await syncs.create_sync(
        syncs.CreateSyncRule(
            migration_job_id="manual-job",
            mode="add_only",
            cadence_minutes=60,
            timezone="UTC",
        ),
        session=session,
        user_id="local",
    )

    checkpoint = await session.get(orm.SyncCheckpoint, view.id)
    assert checkpoint is not None
    assert len(checkpoint.source_snapshot["tracks"]) == 2
    assert list(checkpoint.mappings.values()) == [
        "target:track:one",
        "target:track:two",
    ]
    assert view.next_run_at is not None
    assert view.enabled is True


async def test_create_mirror_sync_rejects_target_without_mirror_support(
    session,
    monkeypatch,
) -> None:
    await _completed_migration(session)
    adapters = _adapters()
    monkeypatch.setattr(syncs, "get", adapters.__getitem__)
    monkeypatch.setattr(syncs, "load_fresh_credential", _fake_credential)

    with pytest.raises(HTTPException) as error:
        await syncs.create_sync(
            syncs.CreateSyncRule(
                migration_job_id="manual-job",
                mode="mirror",
                cadence_minutes=60,
                timezone="UTC",
            ),
            session=session,
            user_id="local",
        )

    assert error.value.status_code == 400
    assert "cannot remove and reorder" in str(error.value.detail)


async def test_create_sync_rejects_source_drift_since_migration(
    session,
    monkeypatch,
) -> None:
    await _completed_migration(session)
    adapters = _adapters()
    source = adapters["source"].playlists["source-playlist"]
    adapters["source"].playlists["source-playlist"] = source.model_copy(
        update={
            "tracks": [
                source.tracks[1].model_copy(update={"position": 0}),
                source.tracks[0].model_copy(update={"position": 1}),
            ]
        }
    )
    monkeypatch.setattr(syncs, "get", adapters.__getitem__)
    monkeypatch.setattr(syncs, "load_fresh_credential", _fake_credential)

    with pytest.raises(HTTPException) as error:
        await syncs.create_sync(
            syncs.CreateSyncRule(
                migration_job_id="manual-job",
                mode="add_only",
                cadence_minutes=60,
                timezone="UTC",
            ),
            session=session,
            user_id="local",
        )

    assert error.value.status_code == 409
    assert "changed since" in str(error.value.detail)


async def test_unresolved_review_blocks_manual_run_even_when_rule_is_paused(session) -> None:
    rule = orm.SyncRule(
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
        enabled=False,
        status="paused",
    )
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
    await session.commit()

    with pytest.raises(HTTPException) as error:
        await syncs.run_sync_now(
            rule.id,
            BackgroundTasks(),
            session=session,
            user_id="local",
        )

    assert error.value.status_code == 409
    assert "review" in str(error.value.detail)


async def test_feedback_loop_detection_rejects_longer_cycles(session) -> None:
    def rule(source: str, target: str) -> orm.SyncRule:
        return orm.SyncRule(
            user_id="local",
            source_provider="provider",
            source_account_id="account",
            source_playlist_id=source,
            source_playlist_name=source,
            target_provider="provider",
            target_account_id="account",
            target_playlist_id=target,
            target_playlist_name=target,
            mode="add_only",
            cadence_minutes=60,
            timezone="UTC",
            enabled=True,
            status="idle",
        )

    session.add_all([rule("a", "b"), rule("b", "c")])
    await session.commit()

    with pytest.raises(HTTPException) as error:
        await syncs._ensure_no_feedback_loop(
            session,
            user_id="local",
            source_provider="provider",
            source_account_id="account",
            source_playlist_id="c",
            target_provider="provider",
            target_account_id="account",
            target_playlist_id="a",
        )

    assert error.value.status_code == 409
    assert "feedback loop" in str(error.value.detail)
