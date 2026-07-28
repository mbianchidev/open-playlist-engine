from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import ProviderAdapter, ProviderCredential
from app.core.capabilities import Capability
from app.core.migration_state import track_selected
from app.core.models import Playlist, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.repositories import load_fresh_credential
from app.imports import IMPORT_RECORD_PROVIDERS
from app.imports.service import (
    LocalImportStateError,
    load_import_for_job,
    load_preview_import,
    selected_import_playlists,
)
from app.settings import Settings
from app.snapshots.bundle import (
    SnapshotCollectionManifest,
    SnapshotIntegrityError,
    SnapshotManifest,
    SnapshotStorage,
)
from app.snapshots.service import snapshot_storage


@dataclass
class MigrationSource:
    kind: str
    provider: str
    account_id: str
    display_name: str
    adapter: ProviderAdapter | None = None
    credential: ProviderCredential | None = None
    local_playlists: dict[str, Playlist] = field(default_factory=dict)
    snapshot: orm.LibrarySnapshot | None = None
    storage: SnapshotStorage | None = None
    manifest: SnapshotManifest | None = None

    @property
    def snapshot_id(self) -> str | None:
        return self.snapshot.id if self.snapshot else None

    @property
    def can_read_tracks(self) -> bool:
        return self.adapter is None or self.adapter.info.capabilities.can(Capability.READ_TRACKS)

    async def read_playlist(self, playlist_id: str) -> Playlist:
        if self.kind == "snapshot":
            if (
                not self.snapshot
                or not self.snapshot.archive_name
                or not self.storage
                or not self.manifest
            ):
                raise SnapshotIntegrityError("snapshot source archive is not available")
            return await asyncio.to_thread(
                self.storage.read_verified_playlist,
                self.snapshot.archive_name,
                self.manifest,
                playlist_id,
            )
        if self.adapter is None:
            playlist = self.local_playlists.get(playlist_id)
            if playlist is None:
                raise LocalImportStateError(
                    f"Playlist '{playlist_id}' is not part of this local import.",
                    code="unknown_playlist",
                )
            return playlist
        if self.credential is None:
            raise RuntimeError(f"source credential missing for provider '{self.provider}'")
        return await self.adapter.read_playlist(
            self.credential,
            PlaylistRef(id=playlist_id, name=playlist_id),
        )

    def collection(self, playlist_id: str) -> SnapshotCollectionManifest | None:
        if not self.manifest:
            return None
        return next(
            (
                collection
                for collection in self.manifest.collections
                if collection.id == playlist_id
            ),
            None,
        )

    def migration_description(self, playlist_id: str) -> str:
        collection = self.collection(playlist_id)
        if collection:
            return (
                f"Restored from a {collection.source_provider} local snapshot "
                "by Open Playlist Engine."
            )
        return f"Migrated from {self.display_name} by Open Playlist Engine."

    async def selected_playlists(
        self,
        *,
        playlist_ids: list[str],
        track_filters: dict[str, list[str]],
    ) -> dict[str, Playlist]:
        if self.adapter is None:
            selected: dict[str, Playlist] = {}
            for playlist_id in playlist_ids:
                playlist = await self.read_playlist(playlist_id)
                wanted = set(track_filters.get(playlist_id) or [])
                selected[playlist_id] = playlist.model_copy(
                    update={
                        "tracks": [
                            track for track in playlist.tracks if track_selected(track, wanted)
                        ]
                    }
                )
            return selected
        selected = {}
        for playlist_id in playlist_ids:
            playlist = await self.read_playlist(playlist_id)
            wanted = set(track_filters.get(playlist_id) or [])
            selected[playlist_id] = playlist.model_copy(
                update={
                    "tracks": [
                        track for track in playlist.tracks if track_selected(track, wanted)
                    ]
                }
            )
        return selected


async def open_migration_source(
    session: AsyncSession,
    *,
    provider: str,
    account_id: str,
    user_id: str,
    settings: Settings,
    job_id: str | None = None,
) -> MigrationSource:
    if provider in IMPORT_RECORD_PROVIDERS:
        record = (
            await load_import_for_job(
                session,
                import_id=account_id,
                user_id=user_id,
                job_id=job_id,
                settings=settings,
            )
            if job_id
            else await load_preview_import(
                session,
                import_id=account_id,
                user_id=user_id,
            )
        )
        playlists = selected_import_playlists(
            record,
            playlist_ids=[
                str(value.get("id"))
                for value in record.playlists or []
                if isinstance(value, dict) and value.get("id")
            ],
            track_filters={},
        )
        return MigrationSource(
            kind="import",
            provider=provider,
            account_id=account_id,
            display_name=record.source_label or "Local file",
            local_playlists=playlists,
        )

    adapter = get(provider)
    credential, _ = await load_fresh_credential(
        session,
        account_id=account_id,
        adapter=adapter,
        provider=provider,
        user_id=user_id,
    )
    return MigrationSource(
        kind="provider",
        provider=provider,
        account_id=account_id,
        display_name=adapter.info.display_name,
        adapter=adapter,
        credential=credential,
    )


async def open_snapshot_source(
    session: AsyncSession,
    *,
    snapshot_id: str,
    user_id: str,
    for_update: bool = True,
) -> MigrationSource:
    snapshot = await session.get(
        orm.LibrarySnapshot,
        snapshot_id,
        with_for_update=for_update,
    )
    if snapshot is None or snapshot.user_id != user_id:
        raise SnapshotIntegrityError("snapshot source was not found")
    if snapshot.status not in {"complete", "partial"} or not snapshot.archive_name:
        raise SnapshotIntegrityError("snapshot source archive is not ready")
    storage = snapshot_storage()
    verified = await asyncio.to_thread(
        storage.verify_archive,
        snapshot.archive_name,
        expected_archive_sha256=snapshot.archive_sha256,
    )
    return MigrationSource(
        kind="snapshot",
        provider="snapshot",
        account_id=f"snapshot:{snapshot.library_id}",
        display_name="Local snapshot",
        snapshot=snapshot,
        storage=storage,
        manifest=verified.manifest,
    )
