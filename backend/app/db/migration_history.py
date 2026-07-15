"""Shared migration-history queries, lifecycle metadata, and retained summaries."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as orm

KNOWN_ITEM_STATUSES = ("pending", "matched", "needs_review", "written", "skipped", "failed")
PROBLEM_STATUSES = ("needs_review", "skipped", "failed")
TERMINAL_JOB_STATUSES = ("done", "failed")


@dataclass(frozen=True, slots=True)
class MigrationItemFilters:
    source_playlist_id: str | None = None
    statuses: tuple[str, ...] = ()
    min_confidence: float | None = None
    max_confidence: float | None = None
    reason: str | None = None
    title: str | None = None
    artist: str | None = None
    problem_only: bool = False


def migration_items_stmt(
    *,
    job_id: str,
    user_id: str,
    filters: MigrationItemFilters,
) -> Select[tuple[orm.JobItem]]:
    return (
        select(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(*_migration_item_conditions(job_id=job_id, user_id=user_id, filters=filters))
        .order_by(
            orm.JobItem.source_playlist_name,
            orm.JobItem.source_playlist_id,
            orm.JobItem.position,
            orm.JobItem.id,
        )
    )


def migration_item_count_stmt(
    *,
    job_id: str,
    user_id: str,
    filters: MigrationItemFilters,
) -> Select[tuple[int]]:
    return (
        select(func.count())
        .select_from(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(*_migration_item_conditions(job_id=job_id, user_id=user_id, filters=filters))
    )


def migration_outcome(job_status: str, counts: Mapping[str, int]) -> str:
    if job_status in {"pending", "running"}:
        return job_status
    if job_status == "failed":
        return "failed"
    if job_status == "done":
        problem_count = sum(int(counts.get(status, 0)) for status in PROBLEM_STATUSES)
        return "partial" if problem_count else "completed"
    return job_status


def mark_job_started(job: orm.MigrationJob, *, now: datetime | None = None) -> None:
    job.status = "running"
    job.error = None
    if job.started_at is None:
        job.started_at = now or utcnow()


def mark_job_terminal(
    job: orm.MigrationJob,
    *,
    status: str,
    retention_days: int,
    now: datetime | None = None,
) -> None:
    if status not in TERMINAL_JOB_STATUSES:
        raise ValueError(f"unsupported terminal migration status: {status}")
    completed_at = now or utcnow()
    job.status = status
    job.completed_at = completed_at
    job.details_expires_at = (
        completed_at + timedelta(days=retention_days) if retention_days > 0 else None
    )


def effective_details_expires_at(
    job: orm.MigrationJob, *, retention_days: int
) -> datetime | None:
    if job.details_expires_at is not None:
        return job.details_expires_at
    if retention_days <= 0 or job.status not in TERMINAL_JOB_STATUSES:
        return None
    completed_at = job.completed_at or job.created_at
    if completed_at is None:
        return None
    return completed_at + timedelta(days=retention_days)


def details_available(
    job: orm.MigrationJob,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> bool:
    if job.details_purged_at is not None:
        return False
    expires_at = effective_details_expires_at(job, retention_days=retention_days)
    return expires_at is None or _comparable_datetime(now or utcnow()) < _comparable_datetime(
        expires_at
    )


def status_counts(
    rows: Iterable[tuple[str, int]], *, total_hint: int = 0
) -> dict[str, Any]:
    raw = Counter({status: int(count) for status, count in rows})
    known = {status: int(raw.get(status, 0)) for status in KNOWN_ITEM_STATUSES}
    other = {
        status: int(count)
        for status, count in raw.items()
        if status not in known and int(count) > 0
    }
    observed = sum(known.values()) + sum(other.values())
    if total_hint > observed:
        known["pending"] += total_hint - observed
    return {"total": max(total_hint, observed), **known, "other": other}


async def collect_job_result_summary(
    session: AsyncSession, job: orm.MigrationJob
) -> dict[str, Any]:
    count_rows = (
        await session.execute(
            select(orm.JobItem.status, func.count())
            .where(orm.JobItem.job_id == job.id)
            .group_by(orm.JobItem.status)
        )
    ).all()
    playlist_rows = (
        await session.execute(
            select(
                orm.JobItem.source_playlist_id,
                func.max(orm.JobItem.source_playlist_name),
                func.max(orm.JobItem.target_playlist_id),
                orm.JobItem.status,
                func.count(),
            )
            .where(orm.JobItem.job_id == job.id)
            .group_by(orm.JobItem.source_playlist_id, orm.JobItem.status)
            .order_by(orm.JobItem.source_playlist_id, orm.JobItem.status)
        )
    ).all()
    grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
    playlist_metadata: dict[str, tuple[str | None, str | None]] = {}
    for playlist_id, playlist_name, target_playlist_id, item_status, count in playlist_rows:
        grouped[playlist_id].append((item_status, int(count)))
        current_name, current_target_id = playlist_metadata.get(playlist_id, (None, None))
        playlist_metadata[playlist_id] = (
            current_name or playlist_name,
            current_target_id or target_playlist_id,
        )

    playlists = []
    for playlist_id, rows in grouped.items():
        playlist_name, target_playlist_id = playlist_metadata[playlist_id]
        playlists.append(
            {
                "source_playlist_id": playlist_id,
                "source_playlist_name": playlist_name,
                "target_playlist_id": target_playlist_id,
                "counts": status_counts(rows),
            }
        )
    return {
        "counts": status_counts(count_rows, total_hint=job.total),
        "playlists": playlists,
    }


def summary_counts(job: orm.MigrationJob) -> dict[str, Any]:
    summary = job.result_summary if isinstance(job.result_summary, dict) else {}
    counts = summary.get("counts")
    if not isinstance(counts, dict):
        return status_counts((), total_hint=job.total)
    return {**status_counts((), total_hint=job.total), **counts}


def summary_playlists(job: orm.MigrationJob) -> list[dict[str, Any]]:
    summary = job.result_summary if isinstance(job.result_summary, dict) else {}
    playlists = summary.get("playlists")
    if not isinstance(playlists, list):
        return []
    return [playlist for playlist in playlists if isinstance(playlist, dict)]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _migration_item_conditions(
    *,
    job_id: str,
    user_id: str,
    filters: MigrationItemFilters,
) -> list[Any]:
    conditions: list[Any] = [
        orm.JobItem.job_id == job_id,
        orm.MigrationJob.user_id == user_id,
    ]
    if filters.source_playlist_id:
        conditions.append(orm.JobItem.source_playlist_id == filters.source_playlist_id)
    if filters.statuses:
        conditions.append(orm.JobItem.status.in_(filters.statuses))
    if filters.problem_only:
        conditions.append(orm.JobItem.status.in_(PROBLEM_STATUSES))
    if filters.min_confidence is not None:
        conditions.append(orm.JobItem.confidence >= filters.min_confidence)
    if filters.max_confidence is not None:
        conditions.append(orm.JobItem.confidence <= filters.max_confidence)
    for column, value in (
        (orm.JobItem.reason, filters.reason),
        (orm.JobItem.title, filters.title),
        (orm.JobItem.artist, filters.artist),
    ):
        if value:
            conditions.append(column.ilike(f"%{_escape_like(value)}%", escape="\\"))
    return conditions


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _comparable_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

