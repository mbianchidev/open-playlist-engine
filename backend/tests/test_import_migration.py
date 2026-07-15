from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import migrations
from app.core.models import MediaType, Playlist, Track
from app.db import models as orm
from app.imports.migration import (
    ImportedMigrationSource,
    ImportSelectionError,
    selected_import_playlists,
)
from app.jobs import migration as worker


def _imported_source() -> ImportedMigrationSource:
    return ImportedMigrationSource(
        import_id="import-1",
        provider="text",
        account_id="import:text",
        label="Pasted text",
        playlist=Playlist(
            id="text:playlist",
            name="Imported list",
            tracks=[
                Track(
                    id="one",
                    title="One",
                    artist="Artist",
                    source_item_id="text:1",
                ),
                Track(
                    id="two",
                    title="Two",
                    artist="Artist",
                    source_item_id="text:2",
                ),
            ],
        ),
    )


def test_create_migration_accepts_exactly_one_source_shape() -> None:
    imported = migrations.CreateMigration(
        source_import_id="import-1",
        target_provider="spotify",
        target_account_id="target",
        selection=migrations.Selection(playlist_ids=["text:playlist"]),
    )
    connected = migrations.CreateMigration(
        source_provider="ytmusic",
        source_account_id="source",
        target_provider="spotify",
        target_account_id="target",
        selection=migrations.Selection(playlist_ids=["playlist"]),
    )

    assert imported.source_import_id == "import-1"
    assert connected.source_provider == "ytmusic"
    with pytest.raises(ValidationError, match="either a connected source"):
        migrations.CreateMigration(
            target_provider="spotify",
            target_account_id="target",
            selection=migrations.Selection(playlist_ids=["playlist"]),
        )


def test_import_selection_reuses_normal_track_filtering() -> None:
    selected = selected_import_playlists(
        _imported_source(),
        playlist_ids=["text:playlist"],
        track_filters={"text:playlist": ["text:2"]},
    )

    assert [track.title for track in selected["text:playlist"].tracks] == ["Two"]
    with pytest.raises(ImportSelectionError):
        selected_import_playlists(
            _imported_source(),
            playlist_ids=["other"],
            track_filters={},
        )


async def test_import_preflight_normalizes_persistent_job_source(monkeypatch) -> None:
    target = SimpleNamespace(info=SimpleNamespace(capabilities=None))
    calls: list[tuple[str, str]] = []

    def fake_get(provider: str):
        assert provider == "spotify"
        return target

    async def fake_load_fresh(
        session,
        *,
        account_id: str,
        adapter,
        provider: str,
        user_id: str,
    ):
        calls.append((provider, user_id))
        return object(), object()

    async def fake_load_import(session, *, import_id: str, user_id: str):
        assert (import_id, user_id) == ("import-1", "user-1")
        return _imported_source()

    async def fake_warnings(session, body, **kwargs):
        assert list(kwargs["selected"]) == ["text:playlist"]
        return []

    monkeypatch.setattr(migrations, "get", fake_get)
    monkeypatch.setattr(migrations, "load_fresh_credential", fake_load_fresh)
    monkeypatch.setattr(migrations, "load_import_source", fake_load_import)
    monkeypatch.setattr(migrations, "_validate_target_capabilities", lambda *args: None)
    monkeypatch.setattr(migrations, "_preflight_warnings", fake_warnings)

    validated = await migrations._validated_preflight_migration(
        object(),
        migrations.CreateMigration(
            source_import_id="import-1",
            target_provider="spotify",
            target_account_id="target",
            selection=migrations.Selection(playlist_ids=["text:playlist"]),
        ),
        user_id="user-1",
    )

    assert validated.source_provider == "text"
    assert validated.source_account_id == "import:text"
    assert validated.selection["source_import_id"] == "import-1"
    assert validated.selection["source_name"] == "Imported list"
    assert calls == [("spotify", "user-1")]


class _WorkerSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


async def test_worker_reads_persisted_import_without_source_credential(monkeypatch) -> None:
    imported = _imported_source()
    imported = ImportedMigrationSource(
        **{
            **imported.__dict__,
            "playlist": imported.playlist.model_copy(
                update={
                    "tracks": [
                        Track(
                            id="episode",
                            title="Podcast",
                            artist="Host",
                            media_type=MediaType.EPISODE,
                            unsupported_reason="episodes are not migratable",
                        )
                    ]
                }
            ),
        }
    )
    target = SimpleNamespace(info=SimpleNamespace(display_name="Spotify"))
    item = orm.JobItem(
        id="item",
        job_id="job",
        source_playlist_id="text:playlist",
        position=0,
        title="Podcast",
        artist="Host",
        status="pending",
    )
    descriptions: list[str] = []

    def fake_get(provider: str):
        assert provider == "spotify"
        return target

    async def fake_load_fresh(session, **kwargs):
        assert kwargs["provider"] == "spotify"
        assert kwargs["user_id"] == "user-1"
        return object(), object()

    async def fake_load_import(session, *, import_id: str, user_id: str):
        assert (import_id, user_id) == ("import-1", "user-1")
        return imported

    async def fake_resolve_target(*args, **kwargs):
        descriptions.append(kwargs["description"])
        return "target-playlist"

    async def fake_create_items(session, job, playlist_id, playlist_name, tracks):
        return [(item, tracks[0])]

    monkeypatch.setattr(worker, "get", fake_get)
    monkeypatch.setattr(worker, "load_fresh_credential", fake_load_fresh)
    monkeypatch.setattr(worker, "load_import_source", fake_load_import)
    monkeypatch.setattr(worker, "_previous_reviewed_items", lambda *args, **kwargs: _async({}))
    monkeypatch.setattr(worker, "_resolve_target_playlist", fake_resolve_target)
    monkeypatch.setattr(worker, "_target_playlist_keys", lambda *args: _async(set()))
    monkeypatch.setattr(worker, "_create_items", fake_create_items)
    monkeypatch.setattr(worker, "commit_job_counts", lambda *args: _async(None))
    monkeypatch.setattr(worker, "_playlist_status_counts", lambda *args: _async({"skipped": 1}))

    job = orm.MigrationJob(
        id="job",
        user_id="user-1",
        source_provider="text",
        source_account_id="import:text",
        target_provider="spotify",
        target_account_id="target",
        selection={
            "playlist_ids": ["text:playlist"],
            "tracks": {},
            "source_import_id": "import-1",
        },
        status="pending",
    )
    await worker._run(_WorkerSession(), job)

    assert item.status == "skipped"
    assert item.reason == "episodes are not migratable"
    assert descriptions == ["Migrated from Pasted text by Open Playlist Engine."]
    assert job.status == "done"


async def _async(value):
    return value
