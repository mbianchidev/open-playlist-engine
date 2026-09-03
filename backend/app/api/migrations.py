"""Migration jobs + live progress (phases 4-5).

Progress is delivered over SSE and derived from persisted ``job_item`` rows, so a
client that reconnects can resume via ``Last-Event-ID`` rather than losing state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    FollowedArtistReader,
    FollowedArtistWriter,
    NotFound,
    ProviderError,
    RateLimited,
    SavedAlbumReader,
    SavedAlbumWriter,
    Unsupported,
)
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
    uri_keys,
)
from app.core.models import MigrationEntityType, Playlist, PlaylistKind, PlaylistRef
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
    summary_entity_counts,
    summary_playlists,
    utcnow,
)
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    load_credential,
    load_fresh_credential,
)
from app.db.sync_graph import lock_sync_graph, pending_continuous_syncs
from app.imports import IMPORT_RECORD_PROVIDERS
from app.imports.service import (
    LocalImportExpired,
    LocalImportNotFound,
    LocalImportStateError,
    queue_import,
)
from app.imports.source import MigrationSource, open_migration_source, open_snapshot_source
from app.jobs.continuous_sync import ensure_continuous_sync
from app.jobs.migration import commit_job_counts, run_migration
from app.jobs.queue import enqueue_or_inline
from app.jobs.sync import finalize_sync_review, review_finalization_ready
from app.settings import get_settings
from app.snapshots.bundle import SnapshotError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/migrations", tags=["migrations"])


class ContinuousSyncIntent(BaseModel):
    mode: Literal["add_only", "mirror"] = "add_only"
    cadence_minutes: int = Field(default=60, ge=1)
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_schedule(self) -> ContinuousSyncIntent:
        settings = get_settings()
        if not (
            settings.sync_min_cadence_minutes
            <= self.cadence_minutes
            <= settings.sync_max_cadence_minutes
        ):
            raise ValueError(
                "cadence_minutes must be between "
                f"{settings.sync_min_cadence_minutes} and "
                f"{settings.sync_max_cadence_minutes}"
            )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA name") from exc
        return self


class Selection(BaseModel):
    playlist_ids: list[str] = Field(default_factory=list)
    # optional per-playlist track filtering: {playlist_id: [track_ids]}
    tracks: dict[str, list[str]] = Field(default_factory=dict)
    saved_album_ids: list[str] = Field(default_factory=list)
    followed_artist_ids: list[str] = Field(default_factory=list)
    continuous_sync: ContinuousSyncIntent | None = None

    def has_items(self) -> bool:
        return bool(self.playlist_ids or self.saved_album_ids or self.followed_artist_ids)


class CreateMigration(BaseModel):
    source_provider: str | None = None
    target_provider: str
    source_account_id: str | None = None
    source_snapshot_id: str | None = None
    target_account_id: str
    selection: Selection
    acknowledge_warnings: bool = False

    @model_validator(mode="after")
    def validate_source(self) -> CreateMigration:
        live = bool(self.source_provider and self.source_account_id)
        partial_live = bool(self.source_provider) != bool(self.source_account_id)
        snapshot = bool(self.source_snapshot_id)
        if partial_live or live == snapshot:
            raise ValueError(
                "provide either source_provider/source_account_id or source_snapshot_id"
            )
        if self.selection.continuous_sync and (
            not live
            or len(self.selection.playlist_ids) != 1
            or any(self.selection.tracks.values())
            or self.selection.saved_album_ids
            or self.selection.followed_artist_ids
        ):
            raise ValueError("continuous sync requires one full provider playlist")
        return self


class JobView(BaseModel):
    id: str
    status: str
    source_kind: str = "provider"
    source_provider: str
    source_snapshot_id: str | None = None
    target_provider: str
    total: int = 0
    done: int = 0
    failed: int = 0
    error: str | None = None
    origin: str = "manual"
    sync_run_id: str | None = None
    match_only: bool = False


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


class MigrationSelectionSummary(BaseModel):
    playlists: int = 0
    tracks: int = 0
    saved_albums: int = 0
    followed_artists: int = 0


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
    selection_summary: MigrationSelectionSummary = Field(
        default_factory=MigrationSelectionSummary
    )


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
    saved_album_count: int = 0
    followed_artist_count: int = 0
    entity_counts: dict[str, StatusCounts] = Field(default_factory=dict)
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
    total_saved_albums: int = 0
    total_followed_artists: int = 0
    entity_counts: dict[str, StatusCounts] = Field(default_factory=dict)
    counts: StatusCounts
    empty: bool = False
    message: str | None = None


class JobItemView(BaseModel):
    id: str
    entity_type: str
    source_playlist_id: str | None = None
    source_playlist_name: str | None = None
    target_playlist_id: str | None = None
    source_entity_id: str | None = None
    source_entity_name: str | None = None
    target_entity_id: str | None = None
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
    warnings: list[dict[str, str]] = Field(default_factory=list)
    summary: MigrationSelectionSummary


@dataclass(slots=True)
class _ValidatedPreflight:
    source: MigrationSource
    warnings: list[dict[str, str]]
    summary: MigrationSelectionSummary


def _job_view(job: orm.MigrationJob) -> JobView:
    return JobView(
        id=job.id,
        status=job.status,
        source_kind=job.source_kind,
        source_provider=job.source_provider,
        source_snapshot_id=job.source_snapshot_id,
        target_provider=job.target_provider,
        total=job.total,
        done=job.done,
        failed=job.failed,
        error=job.error,
        origin=job.origin,
        sync_run_id=job.sync_run_id,
        match_only=bool((job.selection or {}).get("match_only")),
    )


def _item_view(item: orm.JobItem) -> JobItemView:
    return JobItemView(
        id=item.id,
        entity_type=_item_entity_type(item),
        source_playlist_id=item.source_playlist_id,
        source_playlist_name=item.source_playlist_name,
        target_playlist_id=item.target_playlist_id,
        source_entity_id=item.source_entity_id,
        source_entity_name=item.source_entity_name,
        target_entity_id=item.target_entity_id,
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


def _item_entity_type(item: orm.JobItem) -> str:
    return item.entity_type or MigrationEntityType.TRACK.value


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


def _selected_library_ids(job: orm.MigrationJob, field: str) -> list[str]:
    selection = job.selection if isinstance(job.selection, dict) else {}
    raw_ids = selection.get(field)
    if not isinstance(raw_ids, list):
        return []
    return [str(item_id) for item_id in raw_ids if str(item_id).strip()]


def _selection_summary(
    job: orm.MigrationJob, *, track_count: int | None = None
) -> MigrationSelectionSummary:
    selection = job.selection if isinstance(job.selection, dict) else {}
    tracks = selection.get("tracks")
    selected_track_count = (
        sum(len(values) for values in tracks.values() if isinstance(values, list))
        if isinstance(tracks, dict)
        else 0
    )
    return MigrationSelectionSummary(
        playlists=len(_selected_playlist_ids(job)),
        tracks=track_count if track_count is not None else selected_track_count,
        saved_albums=len(_selected_library_ids(job, "saved_album_ids")),
        followed_artists=len(_selected_library_ids(job, "followed_artist_ids")),
    )


def _migration_label(job: orm.MigrationJob, playlist_names: list[str]) -> str:
    library_parts = []
    saved_album_count = len(_selected_library_ids(job, "saved_album_ids"))
    followed_artist_count = len(_selected_library_ids(job, "followed_artist_ids"))
    if saved_album_count:
        library_parts.append(
            f"{saved_album_count} saved album{'s' if saved_album_count != 1 else ''}"
        )
    if followed_artist_count:
        library_parts.append(
            f"{followed_artist_count} artist{'s' if followed_artist_count != 1 else ''}"
        )
    if len(playlist_names) == 1:
        playlist_label = playlist_names[0]
        return ", ".join([playlist_label, *library_parts])
    if len(playlist_names) == 2:
        playlist_label = f"{playlist_names[0]}, {playlist_names[1]}"
        return ", ".join([playlist_label, *library_parts])
    if len(playlist_names) > 2:
        playlist_label = (
            f"{playlist_names[0]}, {playlist_names[1]} + {len(playlist_names) - 2} more"
        )
        return ", ".join([playlist_label, *library_parts])
    selected_count = len(_selected_playlist_ids(job))
    if selected_count == 1:
        return ", ".join(["1 playlist", *library_parts])
    if selected_count > 1:
        playlist_label = f"{selected_count} playlists"
        return ", ".join([playlist_label, *library_parts])
    if library_parts:
        return ", ".join(library_parts)
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
        .where(
            orm.JobItem.job_id.in_(job_ids),
            orm.JobItem.entity_type == MigrationEntityType.TRACK,
            orm.JobItem.source_playlist_id.is_not(None),
        )
        .group_by(orm.JobItem.job_id, orm.JobItem.source_playlist_id)
        .order_by(orm.JobItem.job_id, orm.JobItem.source_playlist_id)
    )
    for job_id, playlist_id, playlist_name in rows.all():
        _append_unique(source_ids_by_job[job_id], playlist_id)
        _append_unique(names_by_job[job_id], playlist_name)

    missing_name_jobs = [job for job in jobs if not names_by_job[job.id]]
    if not missing_name_jobs:
        return names_by_job

    snapshot_jobs = [job for job in missing_name_jobs if job.source_snapshot_id]
    if snapshot_jobs:
        snapshot_rows = list(
            (
                await session.execute(
                    select(orm.LibrarySnapshot).where(
                        orm.LibrarySnapshot.id.in_(
                            [job.source_snapshot_id for job in snapshot_jobs]
                        ),
                        orm.LibrarySnapshot.user_id == user_id,
                    )
                )
            ).scalars()
        )
        manifests = {
            row.id: row.manifest
            for row in snapshot_rows
            if isinstance(row.manifest, dict)
        }
        for job in snapshot_jobs:
            collections = {
                str(collection.get("id")): str(collection.get("name"))
                for collection in manifests.get(job.source_snapshot_id or "", {}).get(
                    "collections", []
                )
                if isinstance(collection, dict)
                and collection.get("id")
                and collection.get("name")
            }
            for playlist_id in _selected_playlist_ids(job):
                _append_unique(names_by_job[job.id], collections.get(playlist_id))

    missing_name_jobs = [job for job in missing_name_jobs if not names_by_job[job.id]]
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
        selection_summary=_selection_summary(job),
    )


def _playlist_stats(items: list[orm.JobItem]) -> list[PlaylistStatsView]:
    grouped: dict[str, list[orm.JobItem]] = defaultdict(list)
    for item in items:
        if (
            _item_entity_type(item) == MigrationEntityType.TRACK
            and item.source_playlist_id is not None
        ):
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


def _entity_status_counts(items: list[orm.JobItem]) -> dict[str, StatusCounts]:
    return {
        entity_type.value: _status_counts_from_items(
            [
                item
                for item in items
                if _item_entity_type(item) == entity_type.value
            ]
        )
        for entity_type in MigrationEntityType
    }


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
    entity_counts = _entity_status_counts(items)
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
        saved_album_count=max(
            entity_counts[MigrationEntityType.ALBUM.value].total,
            len(_selected_library_ids(job, "saved_album_ids")),
        ),
        followed_artist_count=max(
            entity_counts[MigrationEntityType.ARTIST.value].total,
            len(_selected_library_ids(job, "followed_artist_ids")),
        ),
        entity_counts=entity_counts,
        playlists=playlists,
        empty=empty,
        message="No migration items were recorded for this migration yet." if empty else None,
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
    entity_counts = _entity_counts_from_summary(job, summary)
    available = details_available(job, retention_days=retention_days, now=now)
    empty = counts.total == 0
    if not available:
        message = "Item-level migration detail is no longer retained."
    elif empty:
        message = "No migration items were recorded for this migration yet."
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
        saved_album_count=max(
            entity_counts[MigrationEntityType.ALBUM.value].total,
            len(_selected_library_ids(job, "saved_album_ids")),
        ),
        followed_artist_count=max(
            entity_counts[MigrationEntityType.ARTIST.value].total,
            len(_selected_library_ids(job, "followed_artist_ids")),
        ),
        entity_counts=entity_counts,
        playlists=playlists,
        empty=empty,
        message=message,
        detail_available=available,
        detail_expires_at=effective_details_expires_at(job, retention_days=retention_days),
        detail_purged_at=job.details_purged_at,
        retention_days=retention_days,
    )


def _entity_counts_from_summary(
    job: orm.MigrationJob, summary: Mapping[str, object]
) -> dict[str, StatusCounts]:
    raw = summary.get("entity_counts")
    if not isinstance(raw, Mapping):
        raw = {
            MigrationEntityType.TRACK.value: summary.get("counts"),
        }
    return {
        entity_type.value: _status_counts_from_history(
            raw.get(entity_type.value),
            total_hint=(
                len(_selected_library_ids(job, "saved_album_ids"))
                if entity_type is MigrationEntityType.ALBUM
                else len(_selected_library_ids(job, "followed_artist_ids"))
                if entity_type is MigrationEntityType.ARTIST
                else 0
            ),
        )
        for entity_type in MigrationEntityType
    }


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
    entity_counts_by_job: Mapping[str, Mapping[str, Counter[str]]] | None = None,
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
    entity_counts_by_job = entity_counts_by_job or {}
    entity_counts = {}
    for entity_type in MigrationEntityType:
        per_job = []
        for job in jobs:
            selected_hint = 0
            if entity_type is MigrationEntityType.ALBUM:
                selected_hint = len(_selected_library_ids(job, "saved_album_ids"))
            elif entity_type is MigrationEntityType.ARTIST:
                selected_hint = len(_selected_library_ids(job, "followed_artist_ids"))
            per_job.append(
                _status_counts(
                    entity_counts_by_job.get(job.id, {}).get(entity_type.value, Counter()),
                    total_hint=selected_hint,
                )
            )
        entity_counts[entity_type.value] = _sum_status_counts(per_job)
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
        total_saved_albums=entity_counts[MigrationEntityType.ALBUM.value].total,
        total_followed_artists=entity_counts[MigrationEntityType.ARTIST.value].total,
        entity_counts=entity_counts,
        counts=aggregate,
        empty=aggregate.total == 0,
        message=message,
    )


def _migration_filter_conditions(
    *, user_id: str, source_provider: str | None = None, target_provider: str | None = None
) -> list:
    conditions = [
        orm.MigrationJob.user_id == user_id,
        orm.MigrationJob.origin == "manual",
    ]
    if source_provider:
        conditions.append(orm.MigrationJob.source_provider == source_provider)
    if target_provider:
        conditions.append(orm.MigrationJob.target_provider == target_provider)
    return conditions


def _migration_item_filters(
    *,
    source_playlist_id: str | None,
    entity_types: list[MigrationEntityType] | None,
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
        entity_types=tuple(entity_type.value for entity_type in entity_types or []),
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
            orm.JobItem.entity_type,
            orm.JobItem.source_playlist_id,
            orm.JobItem.status,
            func.count(),
        )
        .where(orm.JobItem.job_id.in_(job_ids))
        .group_by(
            orm.JobItem.job_id,
            orm.JobItem.entity_type,
            orm.JobItem.source_playlist_id,
            orm.JobItem.status,
        )
    )


async def _enqueue_or_inline(background_tasks: BackgroundTasks, job_id: str) -> None:
    await enqueue_or_inline(
        background_tasks,
        function_name="run_migration",
        fallback=run_migration,
        job_id=job_id,
        job_label="migration",
    )


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
                .where(
                    orm.MigrationJob.user_id == user_id,
                    orm.MigrationJob.origin == "manual",
                )
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
        for job_id, _entity_type, _playlist_id, item_status, count in rows.all():
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
    if not body.selection.has_items():
        raise HTTPException(status_code=400, detail="Select at least one item to migrate")
    await _ensure_no_continuous_sync_feedback(session, body, user_id=user_id)
    try:
        preflight = await _validated_preflight(session, body, user_id=user_id)
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
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (LocalImportNotFound, LocalImportExpired, LocalImportStateError) as exc:
        raise _local_import_http_error(exc) from exc

    if preflight.warnings and not body.acknowledge_warnings:
        await session.commit()
        raise HTTPException(
            status_code=409,
            detail=MigrationWarningsView(
                warnings=preflight.warnings,
                summary=preflight.summary,
            ).model_dump(),
        )

    if body.selection.continuous_sync is not None:
        await lock_sync_graph(session, user_id)
        await _ensure_no_continuous_sync_feedback(session, body, user_id=user_id)
    job = orm.MigrationJob(
        user_id=user_id,
        source_kind=preflight.source.kind,
        source_provider=preflight.source.provider,
        target_provider=body.target_provider,
        source_account_id=preflight.source.account_id,
        source_snapshot_id=preflight.source.snapshot_id,
        target_account_id=body.target_account_id,
        selection=body.selection.model_dump(),
        status="pending",
        warnings=preflight.warnings,
    )
    session.add(job)
    await session.flush()
    if preflight.source.kind == "import":
        try:
            await queue_import(
                session,
                import_id=preflight.source.account_id,
                user_id=user_id,
                job_id=job.id,
                settings=get_settings(),
            )
        except (LocalImportNotFound, LocalImportExpired, LocalImportStateError) as exc:
            await session.rollback()
            raise _local_import_http_error(exc) from exc
    await session.commit()
    await _enqueue_or_inline(background_tasks, job.id)
    return _job_view(job)


@router.post("/preflight", response_model=MigrationWarningsView)
async def preflight_migration(
    body: CreateMigration,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> MigrationWarningsView:
    if not body.selection.has_items():
        raise HTTPException(status_code=400, detail="Select at least one item to migrate")
    await _ensure_no_continuous_sync_feedback(session, body, user_id=user_id)
    try:
        preflight = await _validated_preflight(session, body, user_id=user_id)
        await session.commit()
        return MigrationWarningsView(
            warnings=preflight.warnings,
            summary=preflight.summary,
        )
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
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (LocalImportNotFound, LocalImportExpired, LocalImportStateError) as exc:
        raise _local_import_http_error(exc) from exc


async def _ensure_no_continuous_sync_feedback(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> None:
    if body.selection.continuous_sync is None:
        return
    source = (
        body.source_provider or "",
        body.source_account_id or "",
        body.selection.playlist_ids[0],
    )
    rows = (
        await session.execute(
            select(
                orm.SyncRule.source_provider,
                orm.SyncRule.source_account_id,
                orm.SyncRule.source_playlist_id,
                orm.SyncRule.target_provider,
                orm.SyncRule.target_account_id,
                orm.SyncRule.target_playlist_id,
            ).where(orm.SyncRule.user_id == user_id)
        )
    ).all()
    adjacency: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for row in rows:
        current = (row.source_provider, row.source_account_id, row.source_playlist_id)
        target = (row.target_provider, row.target_account_id, row.target_playlist_id)
        adjacency.setdefault(current, set()).add(target)

    pending_edges = await pending_continuous_syncs(session, user_id=user_id)
    target_account = (body.target_provider, body.target_account_id)
    if any(
        edge.source == source and edge.target_account == target_account
        for edge in pending_edges
    ):
        raise HTTPException(
            status_code=409,
            detail="continuous sync setup is already pending for this target account",
        )

    account_adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for row in rows:
        account_adjacency.setdefault(
            (row.source_provider, row.source_account_id),
            set(),
        ).add((row.target_provider, row.target_account_id))
    for edge in pending_edges:
        account_adjacency.setdefault(edge.source[:2], set()).add(edge.target_account)

    pending_accounts = [target_account]
    visited_accounts: set[tuple[str, str]] = set()
    while pending_accounts:
        account = pending_accounts.pop()
        if account == source[:2]:
            raise HTTPException(
                status_code=409,
                detail="continuous sync would create a feedback loop",
            )
        if account in visited_accounts:
            continue
        visited_accounts.add(account)
        pending_accounts.extend(account_adjacency.get(account, ()))

    pending = [
        endpoint
        for endpoint in adjacency
        if endpoint[:2] == (body.target_provider, body.target_account_id)
    ]
    visited: set[tuple[str, str, str]] = set()
    while pending:
        endpoint = pending.pop()
        if endpoint == source:
            raise HTTPException(
                status_code=409,
                detail="continuous sync would create a feedback loop",
            )
        if endpoint in visited:
            continue
        visited.add(endpoint)
        pending.extend(adjacency.get(endpoint, ()))


async def _validated_preflight(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> _ValidatedPreflight:
    get(body.target_provider)
    await load_credential(
        session,
        account_id=body.target_account_id,
        provider=body.target_provider,
        user_id=user_id,
    )

    if body.source_snapshot_id:
        if body.selection.saved_album_ids or body.selection.followed_artist_ids:
            raise HTTPException(
                status_code=400,
                detail="Snapshot sources support playlist tracks only",
            )
        source = await open_snapshot_source(
            session,
            snapshot_id=body.source_snapshot_id,
            user_id=user_id,
        )
    else:
        source_provider = body.source_provider or ""
        source_account_id = body.source_account_id or ""
        if source_provider in IMPORT_RECORD_PROVIDERS and (
            body.selection.saved_album_ids or body.selection.followed_artist_ids
        ):
            raise HTTPException(
                status_code=400,
                detail="Import sources support playlist tracks only",
            )
        source = await open_migration_source(
            session,
            provider=source_provider,
            account_id=source_account_id,
            user_id=user_id,
            settings=get_settings(),
        )
    if body.selection.playlist_ids and not source.can_read_tracks:
        raise HTTPException(
            status_code=400,
            detail=f"{source.display_name} cannot read tracks",
        )
    warnings, summary = await _preflight(
        session,
        body,
        source=source,
        user_id=user_id,
    )
    return _ValidatedPreflight(
        source=source,
        warnings=warnings,
        summary=summary,
    )


async def _preflight(
    session: AsyncSession,
    body: CreateMigration,
    *,
    source: MigrationSource,
    user_id: str,
) -> tuple[list[dict[str, str]], MigrationSelectionSummary]:
    settings = get_settings()
    target = get(body.target_provider)
    target_cred, _ = await load_fresh_credential(
        session,
        account_id=body.target_account_id,
        adapter=target,
        provider=body.target_provider,
        user_id=user_id,
    )

    selected = await source.selected_playlists(
        playlist_ids=body.selection.playlist_ids,
        track_filters=body.selection.tracks or {},
    )
    if source.kind == "provider":
        if source.adapter is None or source.credential is None:
            raise ValueError("provider migration source is unavailable")
        await _validate_selected_library_entities(
            source.adapter,
            source.credential,
            body.selection,
        )
    _validate_target_capabilities(target, target_cred, selected, body.selection)
    if body.selection.continuous_sync is not None:
        from app.api.syncs import _ensure_mirror_available, _validate_schedule

        intent = body.selection.continuous_sync
        _validate_schedule(intent.cadence_minutes, intent.timezone)
        if intent.mode == "mirror":
            playlist = selected[body.selection.playlist_ids[0]]
            _ensure_mirror_available(target, playlist.kind)
    total_tracks = sum(len(playlist.tracks) for playlist in selected.values())
    summary = MigrationSelectionSummary(
        playlists=len(body.selection.playlist_ids),
        tracks=total_tracks,
        saved_albums=len(body.selection.saved_album_ids),
        followed_artists=len(body.selection.followed_artist_ids),
    )
    warnings: list[dict[str, str]] = []
    if source.kind == "snapshot":
        for playlist_id in body.selection.playlist_ids:
            collection = source.collection(playlist_id)
            if collection and not collection.complete:
                warnings.append(
                    _warning(
                        "partial_snapshot_collection",
                        f'"{collection.name}" was only partially captured: {collection.error}.',
                    )
                )
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

    unsupported_items = sum(
        not track.is_migratable
        for playlist in selected.values()
        for track in playlist.tracks
    )
    if unsupported_items:
        warnings.append(
            _warning(
                "unsupported_items",
                f"{unsupported_items} selected local or malformed "
                "playlist entries cannot be matched and will be skipped.",
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
    if source.adapter is not None:
        warnings.extend(_artist_semantics_warnings(source.adapter, target, body.selection))
    return warnings, summary


async def _validated_preflight_warnings(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> list[dict[str, str]]:
    preflight = await _validated_preflight(session, body, user_id=user_id)
    return preflight.warnings


async def _selected_playlists(
    source: MigrationSource,
    selection: Selection,
) -> dict[str, Playlist]:
    return await source.selected_playlists(
        playlist_ids=selection.playlist_ids,
        track_filters=selection.tracks or {},
    )


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


def _require_saved_album_reader(adapter) -> SavedAlbumReader:
    if not isinstance(adapter, SavedAlbumReader):
        raise Unsupported(f"{adapter.info.display_name} cannot read saved albums")
    return adapter


def _require_saved_album_writer(adapter) -> SavedAlbumWriter:
    if not isinstance(adapter, SavedAlbumWriter):
        raise Unsupported(f"{adapter.info.display_name} cannot write saved albums")
    return adapter


def _require_followed_artist_reader(adapter) -> FollowedArtistReader:
    if not isinstance(adapter, FollowedArtistReader):
        raise Unsupported(
            f"{adapter.info.display_name} cannot read followed or favorite artists"
        )
    return adapter


def _require_followed_artist_writer(adapter) -> FollowedArtistWriter:
    if not isinstance(adapter, FollowedArtistWriter):
        raise Unsupported(
            f"{adapter.info.display_name} cannot write followed or favorite artists"
        )
    return adapter


async def _validate_selected_library_entities(
    source,
    source_cred,
    selection: Selection,
) -> None:
    if not (selection.saved_album_ids or selection.followed_artist_ids):
        return
    if selection.saved_album_ids:
        album_reader = _require_saved_album_reader(source)
        source.info.require_saved_albums_source(source_cred)
        present = await album_reader.contains_saved_albums(
            source_cred, selection.saved_album_ids
        )
        if len(present) != len(selection.saved_album_ids):
            raise ProviderError("source returned an invalid saved-album membership response")
        for album_id, is_saved in zip(selection.saved_album_ids, present, strict=True):
            if not is_saved:
                raise NotFound(f"saved album is no longer in the source library: {album_id}")
            await album_reader.read_saved_album(source_cred, album_id)
    if selection.followed_artist_ids:
        artist_reader = _require_followed_artist_reader(source)
        source.info.require_followed_artists_source(source_cred)
        present = await artist_reader.contains_followed_artists(
            source_cred, selection.followed_artist_ids
        )
        if len(present) != len(selection.followed_artist_ids):
            raise ProviderError("source returned an invalid artist membership response")
        for artist_id, is_followed in zip(
            selection.followed_artist_ids, present, strict=True
        ):
            if not is_followed:
                raise NotFound(
                    f"artist is no longer in the source library: {artist_id}"
                )
            await artist_reader.read_followed_artist(source_cred, artist_id)


def _artist_semantics_warnings(
    source,
    target,
    selection: Selection,
) -> list[dict[str, str]]:
    if not selection.followed_artist_ids:
        return []
    source_semantics = source.info.artist_collection_semantics
    target_semantics = target.info.artist_collection_semantics
    if not source_semantics or not target_semantics or source_semantics == target_semantics:
        return []
    return [
        _warning(
            "artist_semantics",
            f"{source.info.display_name} {source_semantics.value} artists will become "
            f"{target.info.display_name} {target_semantics.value} artists.",
        )
    ]


def _validate_target_capabilities(
    target,
    target_cred,
    selected: dict[str, Playlist],
    selection: Selection | None = None,
) -> None:
    selection = selection or Selection(playlist_ids=list(selected))
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
    if selection.saved_album_ids:
        _require_saved_album_writer(target)
        target.info.require_saved_albums_target(target_cred)
    if selection.followed_artist_ids:
        _require_followed_artist_writer(target)
        target.info.require_followed_artists_target(target_cred)


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
            orm.JobItem.entity_type == MigrationEntityType.TRACK,
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


def _local_import_http_error(
    exc: LocalImportNotFound | LocalImportExpired | LocalImportStateError,
) -> HTTPException:
    if isinstance(exc, LocalImportNotFound):
        return HTTPException(status_code=404, detail="Local import not found")
    if isinstance(exc, LocalImportExpired):
        return HTTPException(
            status_code=410,
            detail={
                "code": "import_expired",
                "message": "This local import expired. Upload the file again.",
            },
        )
    status_code = 409 if exc.code == "import_queued" else 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


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
    entity_counts_by_job: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    playlist_keys: set[tuple[str, str]] = set()
    if jobs:
        job_ids = [job.id for job in jobs]
        rows = await session.execute(_aggregate_item_counts_stmt(job_ids))
        for job_id, entity_type, playlist_id, status, count in rows.all():
            status_counts_by_job[job_id][status] += int(count)
            normalized_entity_type = entity_type or MigrationEntityType.TRACK.value
            entity_counts_by_job[job_id][normalized_entity_type][status] += int(count)
            if normalized_entity_type == MigrationEntityType.TRACK and playlist_id:
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
            saved_entity_counts = summary_entity_counts(job)
            for entity_type in MigrationEntityType:
                saved = saved_entity_counts[entity_type.value]
                for item_status in _STATUS_FIELDS:
                    entity_counts_by_job[job.id][entity_type.value][item_status] = int(
                        saved.get(item_status, 0) or 0
                    )
                saved_other = saved.get("other")
                if isinstance(saved_other, Mapping):
                    for item_status, count in saved_other.items():
                        entity_counts_by_job[job.id][entity_type.value][str(item_status)] = int(
                            count
                        )
    return _build_aggregate_stats(
        jobs,
        status_counts_by_job,
        playlist_keys,
        entity_counts_by_job,
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
    entity_types: Annotated[
        list[MigrationEntityType] | None, Query(alias="entity_type")
    ] = None,
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
        entity_types=entity_types,
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
    entity_types: Annotated[
        list[MigrationEntityType] | None, Query(alias="entity_type")
    ] = None,
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
        entity_types=entity_types,
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
        "entity_types": list(filters.entity_types),
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
    background_tasks: BackgroundTasks,
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
    view = await _apply_review(session, job, item, body)
    await _maybe_finalize_sync_review(background_tasks, session, job)
    return view


@router.post("/{job_id}/items/review", response_model=list[JobItemView])
async def review_migration_items(
    job_id: str,
    body: BatchReview,
    background_tasks: BackgroundTasks,
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
    await _maybe_finalize_sync_review(background_tasks, session, job)
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
        if job.origin == "sync" and (job.selection or {}).get("match_only"):
            raise HTTPException(
                status_code=400,
                detail="mirror sync tracks cannot be skipped; provide a valid target URI",
            )
        item.status = "skipped"
        item.target_uri = None
        item.target_entity_id = None
        item.reason = "skipped during review"
        await commit_job_counts(session, job)
        return _item_view(item)

    target_uri = (body.target_uri or item.target_uri or "").strip()
    if not target_uri:
        raise HTTPException(status_code=400, detail="target_uri is required to approve a match")
    entity_type = MigrationEntityType(item.entity_type or MigrationEntityType.TRACK)
    if entity_type is not MigrationEntityType.TRACK:
        return await _apply_library_review(
            session,
            job,
            item,
            target_uri,
            entity_type,
        )
    if not item.target_playlist_id:
        raise HTTPException(status_code=400, detail="target playlist is missing for this item")

    try:
        target = get(job.target_provider)
        target_cred, _ = await load_fresh_credential(
            session,
            account_id=job.target_account_id,
            adapter=target,
            provider=job.target_provider,
            user_id=job.user_id,
        )
        if not await target.validate_uri(target_cred, target_uri):
            raise HTTPException(
                status_code=400, detail="target_uri is not valid for target provider"
            )
        if (job.selection or {}).get("match_only"):
            item.target_uri = target_uri
            item.status = "matched"
            item.reason = None
            await commit_job_counts(session, job)
            return _item_view(item)
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


async def _maybe_finalize_sync_review(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    job: orm.MigrationJob,
) -> None:
    if job.origin not in {"manual", "sync"}:
        return
    items = list(
        (
            await session.execute(
                select(orm.JobItem).where(orm.JobItem.job_id == job.id)
            )
        ).scalars()
    )
    if not review_finalization_ready(items):
        return
    if job.origin == "manual":
        await ensure_continuous_sync(session, job)
        return
    if not job.sync_run_id:
        return
    await enqueue_or_inline(
        background_tasks,
        function_name="finalize_sync_review",
        fallback=finalize_sync_review,
        job_id=job.sync_run_id,
        job_label="sync review finalization",
        queue_job_id=f"sync-review:{job.sync_run_id}",
    )


def _review_decision(
    job: orm.MigrationJob, item: orm.JobItem, *, target_uri: str
) -> orm.ReviewDecision:
    metadata = item.source_metadata if isinstance(item.source_metadata, dict) else {}
    entity_type = item.entity_type or MigrationEntityType.TRACK.value
    source_entity_id = item.source_entity_id or _optional_string(metadata.get("id"))
    target_entity_id = item.target_entity_id or _provider_entity_id(target_uri)
    return orm.ReviewDecision(
        job_id=job.id,
        user_id=job.user_id,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        source_account_id=job.source_account_id,
        target_account_id=job.target_account_id,
        entity_type=entity_type,
        source_entity_id=source_entity_id,
        source_entity_name=item.source_entity_name or item.title,
        target_entity_id=target_entity_id,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        isrc=item.isrc,
        source_metadata=metadata,
        target_uri=target_uri,
        confidence=float(item.confidence or 0.0),
        status=item.status,
        action="approve",
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _apply_library_review(
    session: AsyncSession,
    job: orm.MigrationJob,
    item: orm.JobItem,
    target_uri: str,
    entity_type: MigrationEntityType,
) -> JobItemView:
    reviewed_target_id = _provider_entity_id(target_uri)
    try:
        target = get(job.target_provider)
        target_cred, _ = await load_fresh_credential(
            session,
            account_id=job.target_account_id,
            adapter=target,
            provider=job.target_provider,
        )
        if entity_type is MigrationEntityType.ALBUM:
            library = _require_saved_album_writer(target)
            target.info.require_saved_albums_target(target_cred)
            valid = await library.validate_album_uri(target_cred, target_uri)
            present = await library.contains_saved_albums(target_cred, [target_uri])
        else:
            library = _require_followed_artist_writer(target)
            target.info.require_followed_artists_target(target_cred)
            valid = await library.validate_artist_uri(target_cred, target_uri)
            present = await library.contains_followed_artists(target_cred, [target_uri])
        if not valid:
            raise HTTPException(
                status_code=400,
                detail="target_uri is not valid for target provider and entity type",
            )
        if present != [False]:
            if present == [True]:
                item.target_uri = target_uri
                item.target_entity_id = reviewed_target_id
                item.status = "skipped"
                item.reason = (
                    "album already saved in target library"
                    if entity_type is MigrationEntityType.ALBUM
                    else "artist already exists in target library"
                )
                session.add(_review_decision(job, item, target_uri=target_uri))
                await commit_job_counts(session, job)
                return _item_view(item)
            raise ProviderError("target returned an invalid library contains response")
        results = (
            await library.save_albums(target_cred, [target_uri])
            if entity_type is MigrationEntityType.ALBUM
            else await library.follow_artists(target_cred, [target_uri])
        )
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
    operation = (
        "review_save_album"
        if entity_type is MigrationEntityType.ALBUM
        else "review_follow_artist"
    )
    session.add(
        orm.OperationLedger(
            job_id=job.id,
            op=operation,
            intent={"entity_type": entity_type.value, "uri": target_uri},
            observed_target_id=reviewed_target_id
            if result and result.ok
            else None,
            state="done" if result and result.ok else "ambiguous",
        )
    )
    item.target_uri = target_uri
    item.target_entity_id = reviewed_target_id
    if result and result.already_present:
        item.status = "skipped"
        item.reason = (
            "album already saved in target library"
            if entity_type is MigrationEntityType.ALBUM
            else "artist already exists in target library"
        )
    elif result and result.ok:
        item.status = "written"
        item.reason = None
    else:
        item.status = "failed"
        item.reason = (
            (result.error if result else None)
            or "target rejected reviewed library item"
        )
    if item.status in {"written", "skipped"}:
        session.add(_review_decision(job, item, target_uri=target_uri))
    await commit_job_counts(session, job)
    return _item_view(item)


def _provider_entity_id(uri: str) -> str:
    value = uri.strip()
    if ":" in value and "://" not in value:
        return value.rsplit(":", 1)[-1] or value
    parsed = urllib.parse.urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    for item_type in ("track", "album", "artist"):
        if item_type in parts:
            index = parts.index(item_type)
            if index + 1 < len(parts):
                return parts[index + 1]
    return value


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
