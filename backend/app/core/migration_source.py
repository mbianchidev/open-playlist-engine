"""Resolve live provider and verified snapshot inputs behind one read interface."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import ProviderAdapter, ProviderCredential
from app.core.models import Playlist, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.repositories import load_fresh_credential
from app.snapshots.bundle import (
    SnapshotCollectionManifest,
    SnapshotIntegrityError,
    SnapshotManifest,
    SnapshotStorage,
)
from app.snapshots.service import snapshot_storage


@dataclass(slots=True)
class MigrationSource:
    kind: str
    provider: str
    account_id: str
    display_name: str
    adapter: ProviderAdapter | None = None
    credential: ProviderCredential | None = None
    snapshot: orm.LibrarySnapshot | None = None
    storage: SnapshotStorage | None = None
    manifest: SnapshotManifest | None = None

    @property
    def snapshot_id(self) -> str | None:
        return self.snapshot.id if self.snapshot else None

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
        if not self.adapter or not self.credential:
            raise ValueError("live provider source is not configured")
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


async def resolve_live_source(
    session: AsyncSession,
    *,
    provider: str,
    account_id: str,
) -> MigrationSource:
    adapter = get(provider)
    credential, _ = await load_fresh_credential(
        session,
        account_id=account_id,
        adapter=adapter,
        provider=provider,
    )
    return MigrationSource(
        kind="provider",
        provider=provider,
        account_id=account_id,
        display_name=adapter.info.display_name,
        adapter=adapter,
        credential=credential,
    )


async def resolve_snapshot_source(
    session: AsyncSession,
    *,
    snapshot_id: str,
    user_id: str,
) -> MigrationSource:
    snapshot = await session.get(
        orm.LibrarySnapshot,
        snapshot_id,
        with_for_update=True,
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


async def resolve_job_source(
    session: AsyncSession,
    job: orm.MigrationJob,
) -> MigrationSource:
    if job.source_kind == "snapshot":
        if not job.source_snapshot_id:
            raise SnapshotIntegrityError("snapshot source was deleted before restore started")
        return await resolve_snapshot_source(
            session,
            snapshot_id=job.source_snapshot_id,
            user_id=job.user_id,
        )
    return await resolve_live_source(
        session,
        provider=job.source_provider,
        account_id=job.source_account_id,
    )
