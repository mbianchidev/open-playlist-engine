"""Migration jobs + live progress (phases 4-5).

Progress is delivered over SSE and derived from persisted ``job_item`` rows, so a
client that reconnects can resume via ``Last-Event-ID`` rather than losing state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.core.adapter import AccessDenied, AuthExpired, NotFound, ProviderError, RateLimited
from app.core.capabilities import Capability
from app.core.migration_reports import (
    REPORT_VERSION,
    build_report_row,
    csv_header_chunk,
    csv_row_chunk,
    json_report_item_chunk,
    json_report_prefix,
    json_report_suffix,
)
from app.core.migration_state import (
    has_track_overlap,
    keys_from_metadata,
    track_keys,
    track_selected,
    uri_keys,
)
from app.core.models import Playlist, PlaylistKind, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session, get_sessionmaker
from app.db.migration_history import (
    MigrationItemFilters,
    collect_job_result_summary,
    details_available,
    effective_details_expires_at,
    migration_item_count_stmt,
    migration_items_stmt,
    migration_outcome,
    summary_counts,
    summary_playlists,
    utcnow,
)
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    load_credential,
    load_fresh_credential,
)
from app.jobs.migration import commit_job_counts, run_migration
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/migrations", tags=["migrations"])


class Selection(BaseModel):
    playlist_ids: list[str] = []
    # optional per-playlist track filtering: {playlist_id: [track_ids]}
    tracks: dict[str, list[str]] = {}


class CreateMigration(BaseModel):
    source_provider: str
    target_provider: str
    source_account_id: str
    target_account_id: str
    selection: Selection
    acknowledge_warnings: bool = False


class JobView(BaseModel):
    id: str
    status: str
    source_provider: str
    target_provider: str
    total: int = 0
    done: int = 0
    failed: int = 0
    error: str | None = None


class AccountHistoryView(BaseModel):
    id: str
    display_name: str | None = None
    connected: bool = False


class StatusCounts(BaseModel):
    total: int = 0
    pending: int = 0
    matched: int = 0
    needs_review: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    other: dict[str, int] = Field(default_factory=dict)


class MigrationOptionView(BaseModel):
    id: str
    label: str
    playlist_names: list[str] = Field(default_factory=list)
    status: str
    source_provider: str
    target_provider: str
    created_at: datetime | None = None
    outcome: str | None = None
    detail_available: bool = True
    detail_expires_at: datetime | None = None


class PlaylistStatsView(BaseModel):
    source_playlist_id: str
    source_playlist_name: str | None = None
    target_playlist_id: str | None = None
    counts: StatusCounts


class MigrationStatsView(BaseModel):
    id: str
    label: str
    playlist_names: list[str] = Field(default_factory=list)
    status: str
    source_provider: str
    target_provider: str
    created_at: datetime | None = None
    outcome: str | None = None
    source_account: AccountHistoryView | None = None
    target_account: AccountHistoryView | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_s: int | None = None
    warnings: list[dict[str, str]] = Field(default_factory=list)
    error: str | None = None
    counts: StatusCounts
    playlist_count: int = 0
    playlists: list[PlaylistStatsView] = Field(default_factory=list)
    empty: bool = False
    message: str | None = None
    detail_available: bool = True
    detail_expires_at: datetime | None = None
    detail_purged_at: datetime | None = None
    retention_days: int = 0


class AggregateMigrationStatsView(BaseModel):
    source_provider: str | None = None
    target_provider: str | None = None
    total_migrations: int = 0
    total_playlists: int = 0
    counts: StatusCounts
    empty: bool = False
    message: str | None = None


class JobItemView(BaseModel):
    id: str
    source_playlist_id: str
    source_playlist_name: str | None = None
    target_playlist_id: str | None = None
    position: int
    title: str
    artist: str
    album: str | None = None
    duration_s: int | None = None
    release_year: int | None = None
    explicit: bool | None = None
    isrc: str | None = None
    source_metadata: dict = Field(default_factory=dict)
    target_uri: str | None = None
    confidence: float | None = None
    status: str
    reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    review_action: Literal["approve", "skip"] | None = None
    review_original_status: str | None = None
    review_original_reason: str | None = None
    reviewed_at: datetime | None = None


class ReviewItem(BaseModel):
    action: Literal["approve", "skip"]
    target_uri: str | None = None


class BatchReview(BaseModel):
    action: Literal["approve", "skip"]
    item_ids: list[str] = []


class MigrationWarningsView(BaseModel):
    code: str = "migration_warnings"
    message: str = "Review and acknowledge migration warnings before starting."
    warnings: list[dict[str, str]] = []


def _job_view(job: orm.MigrationJob) -> JobView:
    return JobView(
        id=job.id,
        status=job.status,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        total=job.total,
        done=job.done,
        failed=job.failed,
        error=job.error,
    )


def _item_view(item: orm.JobItem) -> JobItemView:
    return JobItemView(
        id=item.id,
        source_playlist_id=item.source_playlist_id,
        source_playlist_name=item.source_playlist_name,
        target_playlist_id=item.target_playlist_id,
        position=item.position,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        release_year=item.release_year,
        explicit=item.explicit,
        isrc=item.isrc,
        source_metadata=item.source_metadata or {},
        target_uri=item.target_uri,
        confidence=item.confidence,
        status=item.status,
        reason=item.reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
        review_action=item.review_action,
        review_original_status=item.review_original_status,
        review_original_reason=item.review_original_reason,
        reviewed_at=item.reviewed_at,
    )


_STATUS_FIELDS = ("pending", "matched", "needs_review", "written", "skipped", "failed")


def _status_counts(statuses: Counter[str], *, total_hint: int = 0) -> StatusCounts:
    known = {status: int(statuses.get(status, 0)) for status in _STATUS_FIELDS}
    other = {
        status: int(count)
        for status, count in statuses.items()
        if status not in known and int(count) > 0
    }
    observed = sum(known.values()) + sum(other.values())
    if total_hint > observed:
        known["pending"] += total_hint - observed
    return StatusCounts(total=max(total_hint, observed), other=other, **known)


def _status_counts_from_items(items: list[orm.JobItem], *, total_hint: int = 0) -> StatusCounts:
    return _status_counts(Counter(item.status for item in items), total_hint=total_hint)


def _sum_status_counts(counts: list[StatusCounts]) -> StatusCounts:
    statuses: Counter[str] = Counter()
    other: Counter[str] = Counter()
    total = 0
    for count in counts:
        total += count.total
        for status in _STATUS_FIELDS:
            statuses[status] += getattr(count, status)
        other.update(count.other)
    return StatusCounts(
        total=total,
        pending=statuses["pending"],
        matched=statuses["matched"],
        needs_review=statuses["needs_review"],
        written=statuses["written"],
        skipped=statuses["skipped"],
        failed=statuses["failed"],
        other=dict(other),
    )


def _append_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _selected_playlist_ids(job: orm.MigrationJob) -> list[str]:
    selection = job.selection if isinstance(job.selection, dict) else {}
    raw_ids = selection.get("playlist_ids")
    if not isinstance(raw_ids, list):
        return []
    return [str(playlist_id) for playlist_id in raw_ids if str(playlist_id).strip()]


def _migration_label(job: orm.MigrationJob, playlist_names: list[str]) -> str:
    if len(playlist_names) == 1:
        return playlist_names[0]
    if len(playlist_names) == 2:
        return f"{playlist_names[0]}, {playlist_names[1]}"
    if len(playlist_names) > 2:
        return f"{playlist_names[0]}, {playlist_names[1]} + {len(playlist_names) - 2} more"
    selected_count = len(_selected_playlist_ids(job))
    if selected_count == 1:
        return "1 playlist"
    if selected_count > 1:
        return f"{selected_count} playlists"
    return "Preparing migration"


async def _playlist_names_by_job(
    session: AsyncSession, jobs: list[orm.MigrationJob], *, user_id: str
) -> dict[str, list[str]]:
    names_by_job: dict[str, list[str]] = {job.id: [] for job in jobs}
    source_ids_by_job: dict[str, list[str]] = {job.id: [] for job in jobs}
    job_ids = [job.id for job in jobs]
    if not job_ids:
        return names_by_job

    rows = await session.execute(
        select(
            orm.JobItem.job_id,
            orm.JobItem.source_playlist_id,
            func.max(orm.JobItem.source_playlist_name),
        )
        .where(orm.JobItem.job_id.in_(job_ids))
        .group_by(orm.JobItem.job_id, orm.JobItem.source_playlist_id)
        .order_by(orm.JobItem.job_id, orm.JobItem.source_playlist_id)
    )
    for job_id, playlist_id, playlist_name in rows.all():
        _append_unique(source_ids_by_job[job_id], playlist_id)
        _append_unique(names_by_job[job_id], playlist_name)

    missing_name_jobs = [job for job in jobs if not names_by_job[job.id]]
    if not missing_name_jobs:
        return names_by_job

    fallback_ids: set[str] = set()
    for job in missing_name_jobs:
        fallback_ids.update(_selected_playlist_ids(job))
        fallback_ids.update(source_ids_by_job[job.id])
    if not fallback_ids:
        return names_by_job

    cache_rows = await session.execute(
        select(
            orm.CachedPlaylistRef.provider,
            orm.CachedPlaylistRef.account_id,
            orm.CachedPlaylistRef.playlist_id,
            orm.CachedPlaylistRef.name,
        ).where(
            orm.CachedPlaylistRef.user_id == user_id,
            orm.CachedPlaylistRef.playlist_id.in_(fallback_ids),
        )
    )
    cached_names = {
        (provider, account_id, playlist_id): name
        for provider, account_id, playlist_id, name in cache_rows.all()
    }
    for job in missing_name_jobs:
        ids = _selected_playlist_ids(job) or source_ids_by_job[job.id]
        for playlist_id in ids:
            _append_unique(
                names_by_job[job.id],
                cached_names.get((job.source_provider, job.source_account_id, playlist_id)),
            )
    return names_by_job


def _migration_option(
    job: orm.MigrationJob,
    playlist_names: list[str],
    counts: StatusCounts | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> MigrationOptionView:
    resolved_counts = counts or _status_counts(Counter(), total_hint=job.total)
    resolved_retention_days = (
        get_settings().migration_history_retention_days
        if retention_days is None
        else retention_days
    )
    return MigrationOptionView(
        id=job.id,
        label=_migration_label(job, playlist_names),
        playlist_names=playlist_names,
        status=job.status,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        created_at=job.created_at,
        outcome=migration_outcome(job.status, resolved_counts.model_dump()),
        detail_available=details_available(
            job,
            retention_days=resolved_retention_days,
            now=now,
        ),
        detail_expires_at=effective_details_expires_at(
            job, retention_days=resolved_retention_days
        ),
    )


def _playlist_stats(items: list[orm.JobItem]) -> list[PlaylistStatsView]:
    grouped: dict[str, list[orm.JobItem]] = defaultdict(list)
    for item in items:
        grouped[item.source_playlist_id].append(item)

    playlists: list[PlaylistStatsView] = []
    for source_playlist_id, rows in sorted(
        grouped.items(),
        key=lambda group: _rows_name(group[1]) or group[0],
    ):
        playlists.append(
            PlaylistStatsView(
                source_playlist_id=source_playlist_id,
                source_playlist_name=_rows_name(rows),
                target_playlist_id=next(
                    (item.target_playlist_id for item in rows if item.target_playlist_id),
                    None,
                ),
                counts=_status_counts_from_items(rows),
            )
        )
    return playlists


def _rows_name(items: list[orm.JobItem]) -> str | None:
    for item in items:
        if item.source_playlist_name:
            return item.source_playlist_name
    return None


def _build_migration_stats(
    job: orm.MigrationJob, items: list[orm.JobItem], playlist_names: list[str]
) -> MigrationStatsView:
    playlists = _playlist_stats(items)
    selected_count = len(_selected_playlist_ids(job))
    empty = len(items) == 0
    return MigrationStatsView(
        id=job.id,
        label=_migration_label(job, playlist_names),
        playlist_names=playlist_names,
        status=job.status,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        created_at=job.created_at,
        counts=_status_counts_from_items(items, total_hint=job.total),
        playlist_count=max(len(playlists), selected_count),
        playlists=playlists,
        empty=empty,
        message="No track items were recorded for this migration yet." if empty else None,
    )


def _build_migration_stats_from_summary(
    job: orm.MigrationJob,
    summary: Mapping[str, object],
    playlist_names: list[str],
    *,
    source_account: AccountHistoryView,
    target_account: AccountHistoryView,
    retention_days: int,
    now: datetime | None = None,
) -> MigrationStatsView:
    counts = _status_counts_from_history(summary.get("counts"), total_hint=job.total)
    playlists = _playlist_stats_from_summary(job, summary, playlist_names)
    available = details_available(job, retention_days=retention_days, now=now)
    empty = counts.total == 0
    if not available:
        message = "Item-level migration detail is no longer retained."
    elif empty:
        message = "No track items were recorded for this migration yet."
    else:
        message = None
    return MigrationStatsView(
        id=job.id,
        label=_migration_label(job, playlist_names),
        playlist_names=playlist_names,
        status=job.status,
        outcome=migration_outcome(job.status, counts.model_dump()),
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        source_account=source_account,
        target_account=target_account,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        duration_s=_job_duration_s(job, now=now),
        warnings=job.warnings or [],
        error=job.error,
        counts=counts,
        playlist_count=max(len(playlists), len(_selected_playlist_ids(job))),
        playlists=playlists,
        empty=empty,
        message=message,
        detail_available=available,
        detail_expires_at=effective_details_expires_at(job, retention_days=retention_days),
        detail_purged_at=job.details_purged_at,
        retention_days=retention_days,
    )


def _status_counts_from_history(value: object, *, total_hint: int = 0) -> StatusCounts:
    if not isinstance(value, Mapping):
        return _status_counts(Counter(), total_hint=total_hint)
    other = value.get("other")
    known = {
        status: int(value.get(status, 0) or 0)
        for status in _STATUS_FIELDS
    }
    parsed_other = (
        {
            str(status): int(count)
            for status, count in other.items()
            if int(count) > 0
        }
        if isinstance(other, Mapping)
        else {}
    )
    observed = sum(known.values()) + sum(parsed_other.values())
    if total_hint > observed:
        known["pending"] += total_hint - observed
    return StatusCounts(
        total=max(int(value.get("total", 0) or 0), total_hint, observed),
        pending=known["pending"],
        matched=known["matched"],
        needs_review=known["needs_review"],
        written=known["written"],
        skipped=known["skipped"],
        failed=known["failed"],
        other=parsed_other,
    )


def _playlist_stats_from_summary(
    job: orm.MigrationJob,
    summary: Mapping[str, object],
    playlist_names: list[str],
) -> list[PlaylistStatsView]:
    raw_playlists = summary.get("playlists")
    playlists: list[PlaylistStatsView] = []
    if isinstance(raw_playlists, list):
        for raw in raw_playlists:
            if not isinstance(raw, Mapping):
                continue
            source_playlist_id = str(raw.get("source_playlist_id") or "")
            if not source_playlist_id:
                continue
            playlists.append(
                PlaylistStatsView(
                    source_playlist_id=source_playlist_id,
                    source_playlist_name=_optional_string(raw.get("source_playlist_name")),
                    target_playlist_id=_optional_string(raw.get("target_playlist_id")),
                    counts=_status_counts_from_history(raw.get("counts")),
                )
            )

    by_id = {playlist.source_playlist_id: playlist for playlist in playlists}
    selected_ids = _selected_playlist_ids(job)
    for index, playlist_id in enumerate(selected_ids):
        if playlist_id in by_id:
            continue
        playlist = PlaylistStatsView(
            source_playlist_id=playlist_id,
            source_playlist_name=playlist_names[index] if index < len(playlist_names) else None,
            counts=StatusCounts(),
        )
        playlists.append(playlist)
        by_id[playlist_id] = playlist
    return sorted(
        playlists,
        key=lambda playlist: playlist.source_playlist_name or playlist.source_playlist_id,
    )


def _job_duration_s(job: orm.MigrationJob, *, now: datetime | None = None) -> int | None:
    if job.started_at is None:
        return None
    end = job.completed_at or now or utcnow()
    started_at = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=UTC)
    resolved_end = end if end.tzinfo else end.replace(tzinfo=UTC)
    return max(0, int((resolved_end - started_at).total_seconds()))


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _build_aggregate_stats(
    jobs: list[orm.MigrationJob],
    status_counts_by_job: Mapping[str, Counter[str]],
    playlist_keys: set[tuple[str, str]],
    *,
    source_provider: str | None,
    target_provider: str | None,
) -> AggregateMigrationStatsView:
    counts = [
        _status_counts(status_counts_by_job.get(job.id, Counter()), total_hint=job.total)
        for job in jobs
    ]
    all_playlist_keys = set(playlist_keys)
    for job in jobs:
        for playlist_id in _selected_playlist_ids(job):
            all_playlist_keys.add((job.id, playlist_id))

    aggregate = _sum_status_counts(counts)
    message = None
    if not jobs:
        message = "No migrations match these filters."
    elif aggregate.total == 0:
        message = "Migrations match these filters, but no track items were recorded yet."

    return AggregateMigrationStatsView(
        source_provider=source_provider,
        target_provider=target_provider,
        total_migrations=len(jobs),
        total_playlists=len(all_playlist_keys),
        counts=aggregate,
        empty=aggregate.total == 0,
        message=message,
    )


def _migration_filter_conditions(
    *, user_id: str, source_provider: str | None = None, target_provider: str | None = None
) -> list:
    conditions = [orm.MigrationJob.user_id == user_id]
    if source_provider:
        conditions.append(orm.MigrationJob.source_provider == source_provider)
    if target_provider:
        conditions.append(orm.MigrationJob.target_provider == target_provider)
    return conditions


def _migration_item_filters(
    *,
    source_playlist_id: str | None,
    statuses: list[str] | None,
    min_confidence: float | None,
    max_confidence: float | None,
    reason: str | None,
    title: str | None,
    artist: str | None,
    problem_only: bool,
) -> MigrationItemFilters:
    if (
        min_confidence is not None
        and max_confidence is not None
        and min_confidence > max_confidence
    ):
        raise HTTPException(
            status_code=400,
            detail="min_confidence cannot be greater than max_confidence",
        )
    normalized_statuses = tuple(
        status.strip() for status in statuses or [] if status.strip()
    )
    if len(normalized_statuses) > 20 or any(len(status) > 50 for status in normalized_statuses):
        raise HTTPException(status_code=400, detail="status filters are invalid")
    return MigrationItemFilters(
        source_playlist_id=source_playlist_id,
        statuses=normalized_statuses,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        reason=reason,
        title=title,
        artist=artist,
        problem_only=problem_only,
    )


def _owned_job_stmt(job_id: str, user_id: str):
    return select(orm.MigrationJob).where(
        orm.MigrationJob.id == job_id,
        orm.MigrationJob.user_id == user_id,
    )


async def _owned_job(
    session: AsyncSession,
    *,
    job_id: str,
    user_id: str,
) -> orm.MigrationJob | None:
    return await session.scalar(_owned_job_stmt(job_id, user_id))


async def _owned_accounts_by_id(
    session: AsyncSession,
    jobs: list[orm.MigrationJob],
    *,
    user_id: str,
) -> dict[str, orm.ProviderAccount]:
    account_ids = {
        account_id
        for job in jobs
        for account_id in (job.source_account_id, job.target_account_id)
        if account_id
    }
    if not account_ids:
        return {}
    accounts = (
        await session.execute(
            select(orm.ProviderAccount).where(
                orm.ProviderAccount.user_id == user_id,
                orm.ProviderAccount.id.in_(account_ids),
            )
        )
    ).scalars()
    return {account.id: account for account in accounts}


def _account_history_view(
    account_id: str,
    provider: str,
    accounts_by_id: Mapping[str, orm.ProviderAccount],
    *,
    user_id: str,
) -> AccountHistoryView:
    account = accounts_by_id.get(account_id)
    connected = (
        account is not None and account.provider == provider and account.user_id == user_id
    )
    return AccountHistoryView(
        id=account_id,
        display_name=account.display_name if connected else None,
        connected=connected,
    )


async def _job_result_summary(
    session: AsyncSession, job: orm.MigrationJob
) -> Mapping[str, object]:
    if job.details_purged_at is not None:
        return job.result_summary or {}
    return await collect_job_result_summary(session, job)


def _require_details_available(job: orm.MigrationJob) -> None:
    retention_days = get_settings().migration_history_retention_days
    if details_available(job, retention_days=retention_days):
        return
    expires_at = effective_details_expires_at(job, retention_days=retention_days)
    if expires_at:
        detail = f"migration item detail expired at {expires_at.isoformat()}"
    else:
        detail = "migration item detail is no longer retained"
    raise HTTPException(status_code=410, detail=detail)


def _initialize_details_expiry(job: orm.MigrationJob, *, retention_days: int) -> bool:
    if job.details_expires_at is not None or retention_days <= 0:
        return False
    expires_at = effective_details_expires_at(job, retention_days=retention_days)
    if expires_at is None:
        return False
    job.details_expires_at = expires_at
    return True


def _aggregate_item_counts_stmt(job_ids: list[str]):
    return (
        select(
            orm.JobItem.job_id,
            orm.JobItem.source_playlist_id,
            orm.JobItem.status,
            func.count(),
        )
        .where(orm.JobItem.job_id.in_(job_ids))
        .group_by(
            orm.JobItem.job_id,
            orm.JobItem.source_playlist_id,
            orm.JobItem.status,
        )
    )


async def _enqueue_or_inline(background_tasks: BackgroundTasks, job_id: str) -> None:
    try:
        redis = await create_pool(RedisSettings.from_dsn(get_settings().valkey_url))
        try:
            await redis.enqueue_job("run_migration", job_id)
        finally:
            await redis.close(close_connection_pool=True)
    except (ConnectionError, OSError, RedisError, TimeoutError) as exc:
        logger.warning(
            "queue unavailable; running migration inline job_id=%s error=%s", job_id, exc
        )
        background_tasks.add_task(run_migration, {}, job_id)


@router.get("", response_model=list[MigrationOptionView])
async def list_migrations(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[MigrationOptionView]:
    retention_days = get_settings().migration_history_retention_days
    jobs = list(
        (
            await session.execute(
                select(orm.MigrationJob)
                .where(orm.MigrationJob.user_id == user_id)
                .order_by(orm.MigrationJob.created_at.desc(), orm.MigrationJob.id.desc())
            )
        ).scalars()
    )
    expiry_changed = False
    for job in jobs:
        expiry_changed = (
            _initialize_details_expiry(job, retention_days=retention_days) or expiry_changed
        )
    if expiry_changed:
        await session.commit()
    names_by_job = await _playlist_names_by_job(session, jobs, user_id=user_id)
    status_counts_by_job: dict[str, Counter[str]] = defaultdict(Counter)
    if jobs:
        rows = await session.execute(_aggregate_item_counts_stmt([job.id for job in jobs]))
        for job_id, _playlist_id, item_status, count in rows.all():
            status_counts_by_job[job_id][item_status] += int(count)
    options = []
    for job in jobs:
        live_counts = status_counts_by_job.get(job.id, Counter())
        counts = (
            _status_counts(live_counts, total_hint=job.total)
            if live_counts
            else _status_counts_from_history(summary_counts(job), total_hint=job.total)
        )
        options.append(
            _migration_option(
                job,
                names_by_job[job.id],
                counts,
                retention_days=retention_days,
            )
        )
    return options


@router.post("", response_model=JobView)
async def create_migration(
    body: CreateMigration,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> JobView:
    if not body.selection.playlist_ids:
        raise HTTPException(status_code=400, detail="Select at least one playlist to migrate")
    try:
        warnings = await _validated_preflight_warnings(session, body, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if warnings and not body.acknowledge_warnings:
        await session.commit()
        raise HTTPException(
            status_code=409,
            detail=MigrationWarningsView(warnings=warnings).model_dump(),
        )

    job = orm.MigrationJob(
        user_id=user_id,
        source_provider=body.source_provider,
        target_provider=body.target_provider,
        source_account_id=body.source_account_id,
        target_account_id=body.target_account_id,
        selection=body.selection.model_dump(),
        status="pending",
        warnings=warnings,
    )
    session.add(job)
    await session.commit()
    await _enqueue_or_inline(background_tasks, job.id)
    return _job_view(job)


@router.post("/preflight", response_model=MigrationWarningsView)
async def preflight_migration(
    body: CreateMigration,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> MigrationWarningsView:
    if not body.selection.playlist_ids:
        raise HTTPException(status_code=400, detail="Select at least one playlist to migrate")
    try:
        warnings = await _validated_preflight_warnings(session, body, user_id=user_id)
        await session.commit()
        return MigrationWarningsView(warnings=warnings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _validated_preflight_warnings(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> list[dict[str, str]]:
    source = get(body.source_provider)
    get(body.target_provider)

    source_caps = source.info.capabilities
    if not source_caps.can(Capability.READ_TRACKS):
        raise HTTPException(status_code=400, detail=f"{body.source_provider} cannot read tracks")
    await load_credential(
        session, account_id=body.source_account_id, provider=body.source_provider
    )
    await load_credential(
        session, account_id=body.target_account_id, provider=body.target_provider
    )
    return await _preflight_warnings(session, body, user_id=user_id)


async def _preflight_warnings(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> list[dict[str, str]]:
    settings = get_settings()
    source = get(body.source_provider)
    target = get(body.target_provider)
    source_cred, _ = await load_fresh_credential(
        session,
        account_id=body.source_account_id,
        adapter=source,
        provider=body.source_provider,
    )
    target_cred, _ = await load_fresh_credential(
        session,
        account_id=body.target_account_id,
        adapter=target,
        provider=body.target_provider,
    )

    selected = await _selected_playlists(source, source_cred, body.selection)
    _validate_target_capabilities(target, target_cred, selected)
    total_tracks = sum(len(playlist.tracks) for playlist in selected.values())
    warnings: list[dict[str, str]] = []
    if len(body.selection.playlist_ids) > settings.migration_safe_max_playlists_per_job:
        warnings.append(
            _warning(
                "playlist_count",
                "Safe default is 1 playlist per job. Start a single playlist unless "
                "you accept the extra account-risk.",
            )
        )
    if total_tracks > settings.migration_safe_max_tracks_per_job:
        warnings.append(
            _warning(
                "track_count",
                f"Safe default is {settings.migration_safe_max_tracks_per_job} tracks "
                f"per job; this job has {total_tracks}.",
            )
        )

    migrated_today = await _tracks_migrated_today(
        session,
        user_id=user_id,
        target_provider=body.target_provider,
        target_account_id=body.target_account_id,
    )
    if migrated_today + total_tracks > settings.migration_safe_daily_tracks:
        warnings.append(
            _warning(
                "daily_limit",
                f"Safe default is {settings.migration_safe_daily_tracks} tracks/day; "
                f"today would reach {migrated_today + total_tracks}.",
            )
        )

    wait_remaining = await _job_wait_remaining(
        session,
        user_id=user_id,
        target_provider=body.target_provider,
        target_account_id=body.target_account_id,
        min_gap_s=settings.migration_safe_min_job_gap_s,
    )
    if wait_remaining > 0:
        warnings.append(
            _warning(
                "job_spacing",
                "Safe default is waiting at least "
                f"{settings.migration_safe_min_job_gap_s // 60} minutes between jobs; "
                f"wait about {wait_remaining} seconds.",
            )
        )

    warnings.extend(await _same_name_warnings(target, target_cred, selected))
    return warnings


async def _selected_playlists(
    source, source_cred, selection: Selection
) -> dict[str, Playlist]:
    selected: dict[str, Playlist] = {}
    track_filters = selection.tracks or {}
    for playlist_id in selection.playlist_ids:
        playlist = await source.read_playlist(
            source_cred, PlaylistRef(id=playlist_id, name=playlist_id)
        )
        wanted = set(track_filters.get(playlist_id) or [])
        tracks = [track for track in playlist.tracks if track_selected(track, wanted)]
        selected[playlist_id] = playlist.model_copy(update={"tracks": tracks})
    return selected


async def _same_name_warnings(
    target, target_cred, selected: dict[str, Playlist]
) -> list[dict[str, str]]:
    target_refs = [ref async for ref in target.iter_playlists(target_cred)]
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
                target_playlist = await target.read_playlist(target_cred, ref)
            except NotFound:
                logger.warning(
                    "skipping unreadable same-name target playlist playlist_id=%s", ref.id
                )
                continue
            if target_playlist.tracks and not has_track_overlap(
                source_playlist.tracks, target_playlist.tracks
            ):
                warnings.append(
                    _warning(
                        "same_name_different_tracks",
                        f'Target already has a playlist named "{source_playlist.name}" '
                        "with different songs.",
                    )
                )
                break
    return warnings


def _validate_target_capabilities(
    target, target_cred, selected: dict[str, Playlist]
) -> None:
    kinds = {playlist.kind for playlist in selected.values()}
    if PlaylistKind.STANDARD in kinds:
        caps = target.info.capabilities
        if not (
            caps.can(Capability.CREATE_PLAYLIST) and caps.can(Capability.ADD_TRACKS)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"{target.info.display_name} cannot write playlists",
            )
    if PlaylistKind.LIKED_TRACKS in kinds:
        target.info.require_liked_tracks_target(target_cred)


async def _tracks_migrated_today(
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


async def _job_wait_remaining(
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


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.get("/stats", response_model=AggregateMigrationStatsView)
async def get_aggregate_migration_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    source_provider: str | None = None,
    target_provider: str | None = None,
) -> AggregateMigrationStatsView:
    conditions = _migration_filter_conditions(
        user_id=user_id,
        source_provider=source_provider,
        target_provider=target_provider,
    )
    jobs = list(
        (
            await session.execute(
                select(orm.MigrationJob)
                .where(*conditions)
                .order_by(orm.MigrationJob.created_at.desc(), orm.MigrationJob.id.desc())
            )
        ).scalars()
    )
    status_counts_by_job: dict[str, Counter[str]] = defaultdict(Counter)
    playlist_keys: set[tuple[str, str]] = set()
    if jobs:
        job_ids = [job.id for job in jobs]
        rows = await session.execute(_aggregate_item_counts_stmt(job_ids))
        for job_id, playlist_id, status, count in rows.all():
            status_counts_by_job[job_id][status] += int(count)
            playlist_keys.add((job_id, playlist_id))
        for job in jobs:
            if status_counts_by_job[job.id]:
                continue
            saved_counts = summary_counts(job)
            for item_status in _STATUS_FIELDS:
                status_counts_by_job[job.id][item_status] = int(
                    saved_counts.get(item_status, 0) or 0
                )
            saved_other = saved_counts.get("other")
            if isinstance(saved_other, Mapping):
                for item_status, count in saved_other.items():
                    status_counts_by_job[job.id][str(item_status)] = int(count)
            for playlist in summary_playlists(job):
                playlist_id = str(playlist.get("source_playlist_id") or "")
                if playlist_id:
                    playlist_keys.add((job.id, playlist_id))
    return _build_aggregate_stats(
        jobs,
        status_counts_by_job,
        playlist_keys,
        source_provider=source_provider,
        target_provider=target_provider,
    )


@router.get("/{job_id}/stats", response_model=MigrationStatsView)
async def get_migration_stats(
    job_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> MigrationStatsView:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    retention_days = get_settings().migration_history_retention_days
    if _initialize_details_expiry(job, retention_days=retention_days):
        await session.commit()
    summary = await _job_result_summary(session, job)
    names_by_job = await _playlist_names_by_job(session, [job], user_id=user_id)
    accounts_by_id = await _owned_accounts_by_id(session, [job], user_id=user_id)
    return _build_migration_stats_from_summary(
        job,
        summary,
        names_by_job[job.id],
        source_account=_account_history_view(
            job.source_account_id,
            job.source_provider,
            accounts_by_id,
            user_id=user_id,
        ),
        target_account=_account_history_view(
            job.target_account_id,
            job.target_provider,
            accounts_by_id,
            user_id=user_id,
        ),
        retention_days=retention_days,
    )


@router.get("/{job_id}", response_model=JobView)
async def get_migration(
    job_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> JobView:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    return _job_view(job)


@router.get("/{job_id}/items", response_model=list[JobItemView])
async def get_migration_items(
    job_id: str,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    source_playlist_id: str | None = None,
    statuses: Annotated[list[str] | None, Query(alias="status")] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    max_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    reason: Annotated[str | None, Query(max_length=200)] = None,
    title: Annotated[str | None, Query(max_length=200)] = None,
    artist: Annotated[str | None, Query(max_length=200)] = None,
    problem_only: bool = False,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobItemView]:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    _require_details_available(job)
    filters = _migration_item_filters(
        source_playlist_id=source_playlist_id,
        statuses=statuses,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        reason=reason,
        title=title,
        artist=artist,
        problem_only=problem_only,
    )
    stmt = migration_items_stmt(job_id=job_id, user_id=user_id, filters=filters)
    total = int(
        await session.scalar(
            migration_item_count_stmt(job_id=job_id, user_id=user_id, filters=filters)
        )
        or 0
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return [_item_view(item) for item in (await session.execute(stmt)).scalars()]


@router.get("/{job_id}/report")
async def download_migration_report(
    job_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    report_format: Annotated[Literal["csv", "json"], Query(alias="format")] = "csv",
    scope: Literal["all", "problems"] = "all",
    source_playlist_id: str | None = None,
    statuses: Annotated[list[str] | None, Query(alias="status")] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    max_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    reason: Annotated[str | None, Query(max_length=200)] = None,
    title: Annotated[str | None, Query(max_length=200)] = None,
    artist: Annotated[str | None, Query(max_length=200)] = None,
) -> StreamingResponse:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    retention_days = get_settings().migration_history_retention_days
    if _initialize_details_expiry(job, retention_days=retention_days):
        await session.commit()
    _require_details_available(job)
    filters = _migration_item_filters(
        source_playlist_id=source_playlist_id,
        statuses=statuses,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        reason=reason,
        title=title,
        artist=artist,
        problem_only=scope == "problems",
    )
    summary = await _job_result_summary(session, job)
    counts = _status_counts_from_history(summary.get("counts"), total_hint=job.total)
    outcome = migration_outcome(job.status, counts.model_dump())
    metadata = {
        "report_version": REPORT_VERSION,
        "job_id": job.id,
        "job_status": job.status,
        "job_outcome": outcome,
        "scope": scope,
        "filters": _report_filters(filters),
        "generated_at": utcnow().isoformat(),
    }
    extension = "csv" if report_format == "csv" else "json"
    filename = _report_filename(job.id, scope=scope, extension=extension)
    media_type = (
        "text/csv; charset=utf-8"
        if report_format == "csv"
        else "application/json; charset=utf-8"
    )
    return StreamingResponse(
        _migration_report_stream(
            job_id=job.id,
            user_id=user_id,
            report_format=report_format,
            filters=filters,
            metadata=metadata,
            outcome=outcome,
        ),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _migration_report_stream(
    *,
    job_id: str,
    user_id: str,
    report_format: Literal["csv", "json"],
    filters: MigrationItemFilters,
    metadata: Mapping[str, object],
    outcome: str,
) -> AsyncIterator[bytes]:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        job = await _owned_job(session, job_id=job_id, user_id=user_id)
        if job is None:
            raise RuntimeError("authorized migration disappeared before report generation")
        stmt = migration_items_stmt(job_id=job_id, user_id=user_id, filters=filters)
        result = await session.stream_scalars(
            stmt.execution_options(yield_per=settings.migration_report_batch_size)
        )
        try:
            if report_format == "csv":
                yield csv_header_chunk()
                async for item in result:
                    yield csv_row_chunk(build_report_row(job, item, outcome=outcome))
                return

            yield json_report_prefix(metadata)
            first = True
            async for item in result:
                yield json_report_item_chunk(
                    build_report_row(job, item, outcome=outcome),
                    first=first,
                )
                first = False
            yield json_report_suffix()
        finally:
            await result.close()


def _report_filters(filters: MigrationItemFilters) -> dict[str, object]:
    return {
        "source_playlist_id": filters.source_playlist_id,
        "statuses": list(filters.statuses),
        "min_confidence": filters.min_confidence,
        "max_confidence": filters.max_confidence,
        "reason": filters.reason,
        "title": filters.title,
        "artist": filters.artist,
        "problem_only": filters.problem_only,
    }


def _report_filename(job_id: str, *, scope: str, extension: str) -> str:
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id).strip("-")[:48] or "migration"
    return f"migration-{safe_job_id}-{scope}.{extension}"


@router.post("/{job_id}/items/{item_id}/review", response_model=JobItemView)
async def review_migration_item(
    job_id: str,
    item_id: str,
    body: ReviewItem,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> JobItemView:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    _require_details_available(job)
    item = await session.get(orm.JobItem, item_id)
    if item is None or item.job_id != job_id:
        raise HTTPException(status_code=404, detail="migration item not found")
    return await _apply_review(session, job, item, body)


@router.post("/{job_id}/items/review", response_model=list[JobItemView])
async def review_migration_items(
    job_id: str,
    body: BatchReview,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[JobItemView]:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    _require_details_available(job)
    if not body.item_ids:
        raise HTTPException(status_code=400, detail="Select at least one migration item")
    stmt = select(orm.JobItem).where(
        orm.JobItem.job_id == job_id,
        orm.JobItem.id.in_(body.item_ids),
    )
    items = list((await session.execute(stmt)).scalars())
    found_ids = {item.id for item in items}
    missing = [item_id for item_id in body.item_ids if item_id not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"migration item not found: {missing[0]}")
    updated = []
    for item in items:
        updated.append(
            await _apply_review(
                session,
                job,
                item,
                ReviewItem(action=body.action, target_uri=item.target_uri),
            )
        )
    return updated


async def _apply_review(
    session: AsyncSession,
    job: orm.MigrationJob,
    item: orm.JobItem,
    body: ReviewItem,
) -> JobItemView:
    if item.status not in {"needs_review", "failed"}:
        raise HTTPException(status_code=400, detail=f"item is already {item.status}")

    original_status = item.status
    original_reason = item.reason
    item.review_action = body.action
    item.review_original_status = original_status
    item.review_original_reason = original_reason
    item.reviewed_at = _utcnow()

    if body.action == "skip":
        item.status = "skipped"
        item.target_uri = None
        item.reason = "skipped during review"
        await commit_job_counts(session, job)
        return _item_view(item)

    target_uri = (body.target_uri or item.target_uri or "").strip()
    if not target_uri:
        raise HTTPException(status_code=400, detail="target_uri is required to approve a match")
    if not item.target_playlist_id:
        raise HTTPException(status_code=400, detail="target playlist is missing for this item")

    try:
        target = get(job.target_provider)
        target_cred, _ = await load_fresh_credential(
            session,
            account_id=job.target_account_id,
            adapter=target,
            provider=job.target_provider,
        )
        if not await target.validate_uri(target_cred, target_uri):
            raise HTTPException(
                status_code=400, detail="target_uri is not valid for target provider"
            )
        existing_keys = await _target_playlist_keys(target, target_cred, item.target_playlist_id)
        duplicate_keys = _item_target_keys(item, target_uri)
        if duplicate_keys & existing_keys:
            item.target_uri = target_uri
            item.status = "skipped"
            item.reason = "duplicate already exists in target playlist"
            session.add(_review_decision(job, item, target_uri=target_uri))
            await commit_job_counts(session, job)
            return _item_view(item)
        results = await target.add_tracks(target_cred, item.target_playlist_id, [target_uri])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = results[0] if results else None
    session.add(
        orm.OperationLedger(
            job_id=job.id,
            op="review_add_track",
            intent={"playlist_id": item.target_playlist_id, "uri": target_uri},
            observed_target_id=item.target_playlist_id if result and result.ok else None,
            position=result.position if result else None,
            state="done" if result and result.ok else "ambiguous",
        )
    )
    item.target_uri = target_uri
    if result and result.ok:
        item.status = "written"
        item.reason = None
        session.add(_review_decision(job, item, target_uri=target_uri))
    else:
        item.status = "failed"
        item.reason = (result.error if result else None) or "target rejected reviewed track"
    await commit_job_counts(session, job)
    return _item_view(item)


def _review_decision(
    job: orm.MigrationJob, item: orm.JobItem, *, target_uri: str
) -> orm.ReviewDecision:
    return orm.ReviewDecision(
        job_id=job.id,
        user_id=job.user_id,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        source_account_id=job.source_account_id,
        target_account_id=job.target_account_id,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        isrc=item.isrc,
        source_metadata=item.source_metadata or {},
        target_uri=target_uri,
        confidence=float(item.confidence or 0.0),
        status=item.status,
        action="approve",
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _target_playlist_keys(target, target_cred, playlist_id: str) -> set[str]:
    try:
        playlist = await target.read_playlist(
            target_cred, PlaylistRef(id=playlist_id, name=playlist_id)
        )
    except NotFound:
        logger.warning(
            "target playlist unavailable while checking duplicates playlist_id=%s",
            playlist_id,
        )
        return set()
    keys: set[str] = set()
    for track in playlist.tracks:
        keys.update(track_keys(track))
    return keys


def _item_target_keys(item: orm.JobItem, target_uri: str | None) -> set[str]:
    keys = uri_keys(target_uri)
    keys.update(
        keys_from_metadata(
            item.source_metadata,
            title=item.title,
            artist=item.artist,
            album=item.album,
            duration_s=item.duration_s,
            isrc=item.isrc,
        )
    )
    return keys


async def _progress_payload(job_id: str, *, user_id: str) -> dict:
    async with get_sessionmaker()() as session:
        job = await _owned_job(session, job_id=job_id, user_id=user_id)
        if job is None:
            return {"job_id": job_id, "missing": True}
        stmt = (
            select(orm.JobItem)
            .where(orm.JobItem.job_id == job_id)
            .order_by(orm.JobItem.source_playlist_id, orm.JobItem.position)
        )
        items = [_item_view(item).model_dump() for item in (await session.execute(stmt)).scalars()]
        return {"job": _job_view(job).model_dump(), "items": items}


async def _event_stream(job_id: str, request: Request, *, user_id: str) -> AsyncIterator[bytes]:
    event_id = 0
    while True:
        if await request.is_disconnected():
            break
        payload = await _progress_payload(job_id, user_id=user_id)
        yield f"id: {event_id}\nevent: progress\ndata: {json.dumps(payload)}\n\n".encode()
        if payload.get("missing"):
            break
        job = payload.get("job")
        if isinstance(job, dict) and job.get("status") in {"done", "failed"}:
            break
        event_id += 1
        await asyncio.sleep(2)


@router.get("/{job_id}/events")
async def migration_events(
    job_id: str,
    request: Request,
    user_id: CurrentUserId,
) -> StreamingResponse:
    async with get_sessionmaker()() as session:
        job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    return StreamingResponse(
        _event_stream(job_id, request, user_id=user_id),
        media_type="text/event-stream",
    )
