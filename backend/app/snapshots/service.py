"""Database and filesystem lifecycle helpers for local snapshots."""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as orm
from app.settings import Settings, get_settings
from app.snapshots.bundle import SnapshotPathError, SnapshotStorage
from app.snapshots.retention import retention_candidates

logger = logging.getLogger(__name__)
_TRASH_NAME = re.compile(
    r"^\.trash-([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\.opb$"
)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    deleted_count: int = 0
    deleted_bytes: int = 0


def snapshot_storage(settings: Settings | None = None) -> SnapshotStorage:
    settings = settings or get_settings()
    return SnapshotStorage(
        settings.snapshot_dir,
        max_archive_bytes=settings.snapshot_import_max_bytes,
        max_uncompressed_bytes=settings.snapshot_max_uncompressed_bytes,
        max_manifest_bytes=settings.snapshot_max_manifest_bytes,
        max_record_bytes=settings.snapshot_max_record_bytes,
        max_compression_ratio=settings.snapshot_max_compression_ratio,
    )


async def delete_snapshot_record(
    session: AsyncSession,
    snapshot: orm.LibrarySnapshot,
    storage: SnapshotStorage,
) -> bool:
    staged_path: Path | None = None
    original_path: Path | None = None
    if snapshot.archive_name:
        original_path = storage.archive_path(snapshot.archive_name)
        if original_path.exists():
            staged_path = _trash_path(storage, snapshot.id)
            if staged_path.exists():
                raise SnapshotPathError("snapshot deletion staging path already exists")
            os.replace(original_path, staged_path)
    try:
        await session.delete(snapshot)
        await session.commit()
    except Exception:
        await session.rollback()
        if staged_path and staged_path.exists() and original_path:
            os.replace(staged_path, original_path)
        raise
    if staged_path:
        staged_path.unlink(missing_ok=True)
    return True


async def cleanup_profile_snapshots(
    session: AsyncSession,
    *,
    profile: orm.SnapshotProfile,
    storage: SnapshotStorage,
    now: datetime | None = None,
) -> CleanupResult:
    rows = list(
        (
            await session.execute(
                select(orm.LibrarySnapshot).where(
                    orm.LibrarySnapshot.profile_id == profile.id,
                    orm.LibrarySnapshot.user_id == profile.user_id,
                )
            )
        ).scalars()
    )
    candidates = retention_candidates(
        rows,
        now=now or datetime.now(UTC),
        retention_count=profile.retention_count,
        retention_days=profile.retention_days,
    )
    deleted_count = 0
    deleted_bytes = 0
    for snapshot in candidates:
        try:
            await delete_snapshot_record(session, snapshot, storage)
        except OSError:
            logger.exception(
                "snapshot retention could not delete archive snapshot_id=%s profile_id=%s",
                snapshot.id,
                profile.id,
            )
            continue
        deleted_count += 1
        deleted_bytes += snapshot.size_bytes
        logger.info(
            "snapshot retention deleted snapshot_id=%s profile_id=%s size_bytes=%s",
            snapshot.id,
            profile.id,
            snapshot.size_bytes,
        )
    return CleanupResult(deleted_count=deleted_count, deleted_bytes=deleted_bytes)


async def reconcile_snapshot_storage(
    session: AsyncSession,
    *,
    storage: SnapshotStorage,
    stale_after_s: int,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=stale_after_s)
    active_rows = list(
        (
            await session.execute(
                select(orm.LibrarySnapshot).where(
                    orm.LibrarySnapshot.status.in_(["pending", "running"])
                )
            )
        ).scalars()
    )
    for snapshot in active_rows:
        updated_at = snapshot.updated_at or snapshot.created_at
        if updated_at and _aware(updated_at) < cutoff:
            snapshot.status = "failed"
            snapshot.errors = [
                {
                    "message": (
                        "snapshot creation was interrupted; start a new snapshot to retry"
                    )
                }
            ]
            snapshot.verification_status = "failed"
            snapshot.verification_error = "snapshot creation was interrupted"
            logger.warning("marked stale snapshot failed snapshot_id=%s", snapshot.id)
    await session.commit()

    rows = list((await session.execute(select(orm.LibrarySnapshot))).scalars())
    by_id = {snapshot.id: snapshot for snapshot in rows}
    referenced = {
        snapshot.archive_name
        for snapshot in rows
        if snapshot.archive_name is not None
    }
    for path in storage.root.iterdir():
        try:
            age_s = now.timestamp() - path.lstat().st_mtime
        except OSError:
            logger.exception("could not inspect snapshot storage entry path=%s", path)
            continue
        if age_s < stale_after_s:
            continue
        trash_match = _TRASH_NAME.fullmatch(path.name)
        if trash_match:
            snapshot = by_id.get(trash_match.group(1))
            if snapshot and snapshot.archive_name:
                final_path = storage.archive_path(snapshot.archive_name)
                if not final_path.exists():
                    os.replace(path, final_path)
                    logger.warning(
                        "restored interrupted snapshot deletion snapshot_id=%s",
                        snapshot.id,
                    )
                    continue
            path.unlink(missing_ok=True)
            continue
        if path.name.startswith(".") and path.suffix == ".tmp":
            path.unlink(missing_ok=True)
            continue
        if path.suffix == ".opb" and path.name not in referenced:
            try:
                storage.archive_path(path.name)
            except SnapshotPathError:
                continue
            path.unlink(missing_ok=True)
            logger.warning("deleted orphan snapshot archive path=%s", path)


def _trash_path(storage: SnapshotStorage, snapshot_id: str) -> Path:
    snapshot_uuid = str(uuid.UUID(snapshot_id))
    path = storage.root / f".trash-{snapshot_uuid}.opb"
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(storage.root):
        raise SnapshotPathError("snapshot trash path escapes the configured directory")
    return path


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
