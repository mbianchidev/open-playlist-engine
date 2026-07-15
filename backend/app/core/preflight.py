"""Shared target-write capability checks and conservative warnings."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import NotFound, ProviderAdapter, ProviderCredential, Unsupported
from app.core.capabilities import Capability
from app.core.migration_state import has_track_overlap
from app.core.models import Playlist, PlaylistKind
from app.db import models as orm
from app.settings import Settings

logger = logging.getLogger(__name__)


def validate_target_capabilities(
    target: ProviderAdapter,
    target_credential: ProviderCredential,
    playlists: list[Playlist],
) -> None:
    kinds = {playlist.kind for playlist in playlists}
    if PlaylistKind.STANDARD in kinds:
        caps = target.info.capabilities
        if not (
            caps.can(Capability.CREATE_PLAYLIST) and caps.can(Capability.ADD_TRACKS)
        ):
            raise Unsupported(f"{target.info.display_name} cannot write playlists")
    if PlaylistKind.LIKED_TRACKS in kinds:
        target.info.require_liked_tracks_target(target_credential)


async def write_preflight_warnings(
    session: AsyncSession,
    *,
    settings: Settings,
    user_id: str,
    target_provider: str,
    target_account_id: str,
    target: ProviderAdapter,
    target_credential: ProviderCredential,
    playlists: list[Playlist],
) -> list[dict[str, str]]:
    validate_target_capabilities(target, target_credential, playlists)
    total_tracks = sum(len(playlist.tracks) for playlist in playlists)
    warnings: list[dict[str, str]] = []
    if len(playlists) > settings.migration_safe_max_playlists_per_job:
        warnings.append(
            warning(
                "playlist_count",
                "Safe default is 1 playlist per job. Start a single playlist unless "
                "you accept the extra account-risk.",
            )
        )
    if total_tracks > settings.migration_safe_max_tracks_per_job:
        warnings.append(
            warning(
                "track_count",
                f"Safe default is {settings.migration_safe_max_tracks_per_job} tracks "
                f"per job; this job has {total_tracks}.",
            )
        )

    migrated_today = await tracks_migrated_today(
        session,
        user_id=user_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
    )
    if migrated_today + total_tracks > settings.migration_safe_daily_tracks:
        warnings.append(
            warning(
                "daily_limit",
                f"Safe default is {settings.migration_safe_daily_tracks} tracks/day; "
                f"today would reach {migrated_today + total_tracks}.",
            )
        )

    wait_remaining = await job_wait_remaining(
        session,
        user_id=user_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
        min_gap_s=settings.migration_safe_min_job_gap_s,
    )
    if wait_remaining > 0:
        warnings.append(
            warning(
                "job_spacing",
                "Safe default is waiting at least "
                f"{settings.migration_safe_min_job_gap_s // 60} minutes between jobs; "
                f"wait about {wait_remaining} seconds.",
            )
        )

    warnings.extend(
        await same_name_warnings(
            target,
            target_credential,
            {str(index): playlist for index, playlist in enumerate(playlists)},
        )
    )
    return warnings


async def same_name_warnings(
    target: ProviderAdapter,
    target_credential: ProviderCredential,
    selected: dict[str, Playlist],
) -> list[dict[str, str]]:
    target_refs = [ref async for ref in target.iter_playlists(target_credential)]
    warnings: list[dict[str, str]] = []
    for source_playlist in selected.values():
        if source_playlist.kind is PlaylistKind.LIKED_TRACKS:
            continue
        same_name = [
            ref
            for ref in target_refs
            if ref.kind is PlaylistKind.STANDARD
            and ref.name.strip() == source_playlist.name.strip()
        ]
        for ref in same_name:
            try:
                target_playlist = await target.read_playlist(target_credential, ref)
            except NotFound:
                logger.warning(
                    "skipping unreadable same-name target playlist playlist_id=%s", ref.id
                )
                continue
            if target_playlist.tracks and not has_track_overlap(
                source_playlist.tracks, target_playlist.tracks
            ):
                warnings.append(
                    warning(
                        "same_name_different_tracks",
                        f'Target already has a playlist named "{source_playlist.name}" '
                        "with different songs.",
                    )
                )
                break
    return warnings


async def tracks_migrated_today(
    session: AsyncSession,
    *,
    user_id: str,
    target_provider: str,
    target_account_id: str,
) -> int:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    count = await session.scalar(
        select(func.count())
        .select_from(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.target_provider == target_provider,
            orm.MigrationJob.target_account_id == target_account_id,
            orm.MigrationJob.created_at >= today,
        )
    )
    return int(count or 0)


async def job_wait_remaining(
    session: AsyncSession,
    *,
    user_id: str,
    target_provider: str,
    target_account_id: str,
    min_gap_s: int,
) -> int:
    job = await session.scalar(
        select(orm.MigrationJob)
        .where(
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.target_provider == target_provider,
            orm.MigrationJob.target_account_id == target_account_id,
        )
        .order_by(orm.MigrationJob.created_at.desc())
        .limit(1)
    )
    if job is None or job.created_at is None:
        return 0
    created_at = job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    elapsed = datetime.now(UTC) - created_at
    remaining = timedelta(seconds=min_gap_s) - elapsed
    return max(0, int(remaining.total_seconds()))


def warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}
