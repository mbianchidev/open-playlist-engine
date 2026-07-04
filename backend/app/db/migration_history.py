"""Read helpers for a user's persisted migration history."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.migration_state import keys_from_metadata
from app.db import models as orm


def is_migrated_item(item: orm.JobItem) -> bool:
    return item.status == "written" or (item.status == "skipped" and bool(item.target_uri))


def migrated_item_keys(item: orm.JobItem) -> set[str]:
    return keys_from_metadata(
        item.source_metadata,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        isrc=item.isrc,
    )


async def migrated_counts(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
) -> dict[str, int]:
    counts: dict[str, set[str]] = {}
    for item in await _migrated_items(
        session,
        user_id=user_id,
        source_provider=source_provider,
        source_account_id=source_account_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
    ):
        keys = migrated_item_keys(item)
        if keys:
            counts.setdefault(item.source_playlist_id, set()).add(sorted(keys)[0])
    return {playlist_id: len(keys) for playlist_id, keys in counts.items()}


async def migrated_track_map(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
    playlist_id: str,
) -> dict[str, tuple[str | None, str | None]]:
    migrated: dict[str, tuple[str | None, str | None]] = {}
    for item in await _migrated_items(
        session,
        user_id=user_id,
        source_provider=source_provider,
        source_account_id=source_account_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
        playlist_id=playlist_id,
    ):
        for key in migrated_item_keys(item):
            migrated[key] = (item.target_playlist_id, item.target_uri)
    return migrated


async def migrated_track_keys(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
    playlist_id: str,
) -> set[str]:
    keys: set[str] = set()
    for item in await _migrated_items(
        session,
        user_id=user_id,
        source_provider=source_provider,
        source_account_id=source_account_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
        playlist_id=playlist_id,
    ):
        keys.update(migrated_item_keys(item))
    return keys


async def _migrated_items(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
    playlist_id: str | None = None,
) -> list[orm.JobItem]:
    stmt = (
        select(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.source_provider == source_provider,
            orm.MigrationJob.source_account_id == source_account_id,
            orm.MigrationJob.target_provider == target_provider,
            orm.MigrationJob.target_account_id == target_account_id,
        )
    )
    if playlist_id is not None:
        stmt = stmt.where(orm.JobItem.source_playlist_id == playlist_id)
    return [
        item
        for item in (await session.execute(stmt)).scalars()
        if is_migrated_item(item) and migrated_item_keys(item)
    ]
