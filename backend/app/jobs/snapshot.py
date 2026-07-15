"""arq job for streamed local library snapshot creation."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.registry import get
from app.db import models as orm
from app.db.base import get_sessionmaker
from app.db.repositories import load_fresh_credential
from app.settings import get_settings
from app.snapshots.bundle import SnapshotSource
from app.snapshots.service import (
    cleanup_profile_snapshots,
    reconcile_snapshot_storage,
    snapshot_storage,
)

logger = logging.getLogger(__name__)


async def run_snapshot(ctx: dict, snapshot_id: str) -> None:
    logger.info("starting snapshot job snapshot_id=%s", snapshot_id)
    storage = snapshot_storage()
    async with get_sessionmaker()() as session:
        snapshot = await session.get(orm.LibrarySnapshot, snapshot_id)
        if snapshot is None:
            logger.error("snapshot job row not found snapshot_id=%s", snapshot_id)
            return
        try:
            profile = await session.scalar(
                select(orm.SnapshotProfile)
                .where(orm.SnapshotProfile.id == snapshot.profile_id)
                .options(selectinload(orm.SnapshotProfile.sources))
            )
            if profile is None:
                raise ValueError("snapshot profile no longer exists")
            snapshot.status = "running"
            snapshot.errors = []
            await session.commit()

            sources: list[SnapshotSource] = []
            for profile_source in sorted(
                profile.sources,
                key=lambda source: (source.provider, source.id),
            ):
                if not profile_source.account_id:
                    raise ValueError(
                        f"{profile_source.provider} account is disconnected; update the profile"
                    )
                adapter = get(profile_source.provider)
                credential, account = await load_fresh_credential(
                    session,
                    account_id=profile_source.account_id,
                    adapter=adapter,
                    provider=profile_source.provider,
                )
                sources.append(
                    SnapshotSource(
                        source_key=profile_source.id,
                        provider=profile_source.provider,
                        account_label=account.display_name or profile_source.account_label,
                        adapter=adapter,
                        credential=credential,
                        collection_ids=list(profile_source.collection_ids or []),
                    )
                )

            result = await storage.create_bundle(
                snapshot_id=snapshot.id,
                library_id=profile.id,
                profile_name=profile.name,
                sources=sources,
                created_at=snapshot.created_at or datetime.now(UTC),
            )
            snapshot.bundle_id = result.manifest.snapshot_id
            snapshot.library_id = result.manifest.library_id
            snapshot.source_providers = [
                source.provider for source in result.manifest.sources
            ]
            snapshot.source_labels = [
                source.account_label
                for source in result.manifest.sources
                if source.account_label
            ]
            snapshot.status = result.manifest.status
            snapshot.schema_version = result.manifest.schema_version
            snapshot.archive_name = result.archive_name
            snapshot.archive_sha256 = result.archive_sha256
            snapshot.size_bytes = result.size_bytes
            snapshot.manifest = result.manifest.model_dump(mode="json")
            snapshot.errors = [
                failure.model_dump(mode="json")
                for failure in result.manifest.failures
            ]
            snapshot.verification_status = "verified"
            snapshot.verification_error = None
            snapshot.verified_at = datetime.now(UTC)
            await session.commit()
            logger.info(
                "snapshot job completed snapshot_id=%s status=%s collections=%s items=%s",
                snapshot.id,
                snapshot.status,
                result.manifest.counts.collections,
                result.manifest.counts.items,
            )
            await cleanup_profile_snapshots(
                session,
                profile=profile,
                storage=storage,
            )
        except asyncio.CancelledError:
            await session.rollback()
            await _mark_snapshot_failed(
                session,
                snapshot_id,
                "snapshot creation was cancelled or timed out",
            )
            logger.exception("snapshot job cancelled or timed out snapshot_id=%s", snapshot_id)
            raise
        except Exception as exc:
            await session.rollback()
            await _mark_snapshot_failed(session, snapshot_id, str(exc))
            logger.exception("snapshot job failed snapshot_id=%s", snapshot_id)


async def snapshot_worker_startup(ctx: dict) -> None:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        await reconcile_snapshot_storage(
            session,
            storage=snapshot_storage(settings),
            stale_after_s=settings.snapshot_stale_after_s,
        )


async def _mark_snapshot_failed(
    session,
    snapshot_id: str,
    error: str,
) -> None:
    snapshot = await session.get(orm.LibrarySnapshot, snapshot_id)
    if snapshot is None:
        return
    snapshot.status = "failed"
    snapshot.errors = [{"message": error}]
    snapshot.verification_status = "failed"
    snapshot.verification_error = error
    await session.commit()
