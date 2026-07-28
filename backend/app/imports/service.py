from __future__ import annotations

import logging
import tempfile
from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.migration_state import track_selected
from app.core.models import Playlist
from app.db import models as orm
from app.db.base import get_sessionmaker
from app.imports.models import (
    ImportIssue,
    ImportLimits,
    ImportParseResult,
    LocalImportPreview,
)
from app.imports.parsers import ImportLimitExceeded
from app.imports.registry import sanitize_filename
from app.settings import Settings

logger = logging.getLogger(__name__)


class LocalImportNotFound(Exception):
    pass


class LocalImportExpired(Exception):
    def __init__(self, import_id: str) -> None:
        super().__init__(
            f"Local import '{import_id}' expired before the migration could claim it."
        )


class LocalImportStateError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


async def spool_upload(
    chunks: AsyncIterable[bytes],
    limits: ImportLimits,
) -> tuple[BinaryIO, int]:
    spool = tempfile.SpooledTemporaryFile(
        max_size=limits.spool_memory_bytes,
        mode="w+b",
    )
    total = 0
    try:
        async for chunk in chunks:
            total += len(chunk)
            if total > limits.max_upload_bytes:
                raise ImportLimitExceeded(
                    f"Upload exceeds the configured {limits.max_upload_bytes}-byte limit.",
                    code="upload_size_limit",
                )
            spool.write(chunk)
        spool.seek(0)
        return spool, total
    except BaseException:
        spool.close()
        raise


async def create_import(
    session: AsyncSession,
    *,
    user_id: str,
    filename: str,
    result: ImportParseResult,
    settings: Settings,
    now: datetime | None = None,
) -> orm.LocalPlaylistImport:
    created_at = now or datetime.now(UTC)
    record = orm.LocalPlaylistImport(
        user_id=user_id,
        filename=sanitize_filename(filename),
        detected_format=result.detected_format.value,
        encoding=result.encoding,
        file_size=result.file_size,
        status="ready",
        playlists=[
            playlist.model_dump(mode="json", exclude_none=False) for playlist in result.playlists
        ],
        issues=[issue.model_dump(mode="json", exclude_none=False) for issue in result.issues],
        limits=settings.local_import_limits.model_dump(mode="json"),
        playlist_count=result.playlist_count,
        track_count=result.track_count,
        duplicate_count=result.duplicate_count,
        malformed_count=result.malformed_count,
        unsupported_count=result.unsupported_count,
        expires_at=created_at + timedelta(seconds=settings.local_import_retention_s),
    )
    session.add(record)
    await session.flush()
    return record


async def load_preview_import(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
    now: datetime | None = None,
) -> orm.LocalPlaylistImport:
    record = await _owned_import(session, import_id=import_id, user_id=user_id)
    current_time = now or datetime.now(UTC)
    if record.status != "ready":
        raise LocalImportStateError(
            "This import has already been queued for migration.",
            code="import_queued",
        )
    if _as_utc(record.expires_at) <= current_time:
        await session.delete(record)
        await session.flush()
        raise LocalImportExpired(import_id)
    return record


async def discard_import(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
) -> None:
    result = await session.execute(
        delete(orm.LocalPlaylistImport).where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.user_id == user_id,
            orm.LocalPlaylistImport.status == "ready",
        )
    )
    if result.rowcount == 1:
        await session.flush()
        return
    record = await session.scalar(
        select(orm.LocalPlaylistImport).where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.user_id == user_id,
        )
    )
    if record is None:
        raise LocalImportNotFound(import_id)
    if record.status != "ready":
        raise LocalImportStateError(
            "A queued import cannot be discarded while its migration is active.",
            code="import_queued",
        )
    raise LocalImportStateError(
        "The import changed state before it could be discarded.",
        code="import_state_changed",
    )


async def queue_import(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
    job_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> orm.LocalPlaylistImport:
    current_time = now or datetime.now(UTC)
    result = await session.execute(
        update(orm.LocalPlaylistImport)
        .where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.user_id == user_id,
            orm.LocalPlaylistImport.status == "ready",
            orm.LocalPlaylistImport.expires_at > current_time,
        )
        .values(
            status="queued",
            queued_job_id=job_id,
            expires_at=current_time
            + timedelta(seconds=settings.local_import_queued_retention_s),
        )
    )
    if result.rowcount != 1:
        record = await session.scalar(
            select(orm.LocalPlaylistImport).where(
                orm.LocalPlaylistImport.id == import_id,
                orm.LocalPlaylistImport.user_id == user_id,
            )
        )
        if record is None:
            raise LocalImportNotFound(import_id)
        if _as_utc(record.expires_at) <= current_time and record.status == "ready":
            await session.delete(record)
            await session.flush()
            raise LocalImportExpired(import_id)
        raise LocalImportStateError(
            "This import is already queued for another migration.",
            code="import_queued",
        )
    return await _owned_import(session, import_id=import_id, user_id=user_id)


async def load_import_for_job(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
    job_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> orm.LocalPlaylistImport:
    current_time = now or datetime.now(UTC)
    result = await session.execute(
        update(orm.LocalPlaylistImport)
        .where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.user_id == user_id,
            orm.LocalPlaylistImport.queued_job_id == job_id,
            orm.LocalPlaylistImport.status.in_(["queued", "failed"]),
            orm.LocalPlaylistImport.expires_at > current_time,
        )
        .values(
            status="queued",
            expires_at=current_time
            + timedelta(seconds=settings.local_import_queued_retention_s),
        )
    )
    if result.rowcount == 1:
        return await _owned_import(session, import_id=import_id, user_id=user_id)
    record = await session.scalar(
        select(orm.LocalPlaylistImport).where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.user_id == user_id,
        )
    )
    if record is None:
        raise LocalImportNotFound(import_id)
    if _as_utc(record.expires_at) <= current_time:
        raise LocalImportExpired(import_id)
    if record.queued_job_id != job_id or record.status not in {"queued", "failed"}:
        raise LocalImportStateError(
            "This local import is not assigned to the requested migration.",
            code="import_job_mismatch",
        )
    raise LocalImportStateError(
        "The local import changed state before the worker could claim it.",
        code="import_state_changed",
    )


async def mark_import_failed(
    session: AsyncSession,
    *,
    import_id: str,
    job_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    record = await session.scalar(
        select(orm.LocalPlaylistImport).where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.queued_job_id == job_id,
        )
    )
    if record is None:
        return
    current_time = now or datetime.now(UTC)
    record.status = "failed"
    record.expires_at = current_time + timedelta(seconds=settings.local_import_failed_retention_s)
    await session.flush()


async def delete_import_for_job(
    session: AsyncSession,
    *,
    import_id: str,
    job_id: str,
) -> None:
    record = await session.scalar(
        select(orm.LocalPlaylistImport).where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.queued_job_id == job_id,
        )
    )
    if record is not None:
        await session.delete(record)
        await session.flush()


async def cleanup_expired_imports(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    current_time = now or datetime.now(UTC)
    expired = list(
        (
            await session.execute(
                select(orm.LocalPlaylistImport).where(
                    orm.LocalPlaylistImport.expires_at <= current_time
                )
            )
        ).scalars()
    )
    deleted = 0
    for record in expired:
        if record.status in {"ready", "failed"}:
            await session.delete(record)
            deleted += 1
            continue
        if record.status != "queued":
            continue
        job = (
            await session.get(orm.MigrationJob, record.queued_job_id)
            if record.queued_job_id
            else None
        )
        if job is not None and job.status in {"pending", "running"}:
            job.status = "failed"
            job.error = (
                "Local import lease expired before the migration completed. "
                "Upload the file and start a new migration."
            )
        await session.delete(record)
        deleted += 1
    await session.flush()
    return deleted


async def cleanup_local_imports(ctx: dict) -> int:
    del ctx
    async with get_sessionmaker()() as session:
        deleted = await cleanup_expired_imports(session)
        await session.commit()
        if deleted:
            logger.info("deleted expired local playlist imports count=%s", deleted)
        return deleted


def preview_from_record(record: orm.LocalPlaylistImport) -> LocalImportPreview:
    return LocalImportPreview(
        id=record.id,
        filename=record.filename,
        detected_format=record.detected_format,
        encoding=record.encoding,
        file_size=record.file_size,
        status=record.status,
        expires_at=_as_utc(record.expires_at),
        playlists=[Playlist.model_validate(value) for value in record.playlists or []],
        issues=[ImportIssue.model_validate(value) for value in record.issues or []],
        playlist_count=record.playlist_count,
        track_count=record.track_count,
        duplicate_count=record.duplicate_count,
        malformed_count=record.malformed_count,
        unsupported_count=record.unsupported_count,
        limits=ImportLimits.model_validate(record.limits or {}),
    )


def selected_import_playlists(
    record: orm.LocalPlaylistImport,
    *,
    playlist_ids: list[str],
    track_filters: dict[str, list[str]],
) -> dict[str, Playlist]:
    available = {
        playlist.id: playlist
        for playlist in (Playlist.model_validate(value) for value in record.playlists or [])
        if playlist.id
    }
    selected: dict[str, Playlist] = {}
    for playlist_id in playlist_ids:
        playlist = available.get(playlist_id)
        if playlist is None:
            raise LocalImportStateError(
                f"Playlist '{playlist_id}' is not part of this local import.",
                code="unknown_playlist",
            )
        wanted = set(track_filters.get(playlist_id) or [])
        selected[playlist_id] = playlist.model_copy(
            update={
                "tracks": [track for track in playlist.tracks if track_selected(track, wanted)]
            }
        )
    return selected


async def _owned_import(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
) -> orm.LocalPlaylistImport:
    record = await session.scalar(
        select(orm.LocalPlaylistImport).where(
            orm.LocalPlaylistImport.id == import_id,
            orm.LocalPlaylistImport.user_id == user_id,
        )
    )
    if record is None:
        raise LocalImportNotFound(import_id)
    return record


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
