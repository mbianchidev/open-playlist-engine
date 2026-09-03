import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import unified_playlists
from app.core.adapter import AuthKind, ProviderCredential, ProviderError
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
    def __init__(self, playlist: Playlist) -> None:
        self.playlist = playlist

    async def iter_playlists(self, credential):
        yield PlaylistRef(
            id=self.playlist.id or "",
            name=self.playlist.name,
            track_count=len(self.playlist.tracks),
        )

    async def read_playlist(self, credential, ref: PlaylistRef) -> Playlist:
        return self.playlist


async def _credential(*args, account_id: str, provider: str, **kwargs):
    return (
        ProviderCredential(
            account_id=account_id,
            provider=provider,
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
        ),
        None,
    )


async def test_unified_playlist_api_groups_connected_accounts_and_sync_links(
    session,
    monkeypatch,
) -> None:
    session.add_all(
        [
            orm.ProviderAccount(
                id="source-account",
                user_id="local",
                provider="source",
                display_name="Source account",
            ),
            orm.ProviderAccount(
                id="target-account",
                user_id="local",
                provider="target",
                display_name="Target account",
            ),
            orm.ProviderAccount(
                id="other-user-account",
                user_id="other",
                provider="source",
                display_name="Other user",
            ),
            orm.SyncRule(
                id="sync-rule",
                user_id="local",
                source_provider="source",
                source_account_id="source-account",
                source_playlist_id="source-playlist",
                source_playlist_name="Canonical",
                target_provider="target",
                target_account_id="target-account",
                target_playlist_id="target-playlist",
                target_playlist_name="Copy",
                mode="add_only",
                cadence_minutes=60,
                timezone="UTC",
            ),
        ]
    )
    await session.commit()
    adapters = {
        "source": _Adapter(
            Playlist(
                id="source-playlist",
                name="Canonical",
                tracks=[
                    Track(
                        id="source-track",
                        title="One",
                        artist="Artist",
                        isrc="USAAA0000001",
                        position=0,
                        provider_uris={"source": "source:track:one"},
                    )
                ],
            )
        ),
        "target": _Adapter(
            Playlist(
                id="target-playlist",
                name="Copy",
                tracks=[
                    Track(
                        id="target-track",
                        title="One",
                        artist="Artist",
                        isrc="USAAA0000001",
                        position=0,
                        provider_uris={"target": "target:track:one"},
                    )
                ],
            )
        ),
    }
    monkeypatch.setattr(unified_playlists, "get", adapters.__getitem__)
    monkeypatch.setattr(unified_playlists, "load_fresh_credential", _credential)

    view = await unified_playlists.list_unified_playlists(
        session=session,
        user_id="local",
        refresh=True,
    )

    assert view.scanned_account_count == 2
    assert view.connected_provider_count == 2
    assert view.warnings == []
    assert len(view.playlists) == 1
    assert view.playlists[0].canonical_member_key == "source:source-account:source-playlist"
    assert view.playlists[0].sync_rule_ids == ["sync-rule"]


class _BrokenAdapter:
    async def iter_playlists(self, credential):
        raise ProviderError("provider unavailable")
        yield


async def test_unified_playlist_api_reports_provider_failures_without_hiding_other_accounts(
    session,
    monkeypatch,
) -> None:
    session.add_all(
        [
            orm.ProviderAccount(
                id="good-account",
                user_id="local",
                provider="good",
                display_name="Good account",
            ),
            orm.ProviderAccount(
                id="bad-account",
                user_id="local",
                provider="bad",
                display_name="Bad account",
            ),
            orm.MigrationJob(
                id="sync-setup-failed",
                user_id="local",
                source_provider="good",
                target_provider="bad",
                source_account_id="good-account",
                target_account_id="bad-account",
                status="done",
                origin="manual",
                selection={
                    "playlist_ids": ["playlist"],
                    "tracks": {},
                    "continuous_sync": {
                        "mode": "add_only",
                        "cadence_minutes": 60,
                        "timezone": "UTC",
                        "status": "active",
                        "sync_rule_id": "deleted-rule",
                        "error": None,
                    },
                },
            ),
        ]
    )
    await session.commit()
    adapters = {
        "good": _Adapter(Playlist(id="playlist", name="Playlist", tracks=[])),
        "bad": _BrokenAdapter(),
    }
    monkeypatch.setattr(unified_playlists, "get", adapters.__getitem__)
    monkeypatch.setattr(unified_playlists, "load_fresh_credential", _credential)

    view = await unified_playlists.list_unified_playlists(
        session=session,
        user_id="local",
        refresh=True,
    )

    assert len(view.playlists) == 1
    assert len(view.warnings) == 1
    assert view.warnings[0].provider == "bad"
    assert "provider unavailable" in view.warnings[0].message
    assert view.playlists[0].sync_attempts[0].status == "failed"
    assert view.playlists[0].sync_attempts[0].source_member_key == "good:good-account:playlist"
    assert view.playlists[0].sync_attempts[0].error == "continuous sync rule was removed"
