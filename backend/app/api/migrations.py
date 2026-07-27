"""Migration jobs + live progress (phases 4-5).

Progress is delivered over SSE and derived from persisted ``job_item`` rows, so a
client that reconnects can resume via ``Last-Event-ID`` rather than losing state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
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
from app.core.migration_state import (
    has_track_overlap,
    keys_from_metadata,
    track_keys,
    track_selected,
    uri_keys,
)
from app.core.models import MigrationEntityType, Playlist, PlaylistKind, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session, get_sessionmaker
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
    playlist_ids: list[str] = Field(default_factory=list)
    # optional per-playlist track filtering: {playlist_id: [track_ids]}
    tracks: dict[str, list[str]] = Field(default_factory=dict)
    saved_album_ids: list[str] = Field(default_factory=list)
    followed_artist_ids: list[str] = Field(default_factory=list)

    def has_items(self) -> bool:
        return bool(self.playlist_ids or self.saved_album_ids or self.followed_artist_ids)


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
    selection_summary: MigrationSelectionSummary


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
    counts: StatusCounts
    playlist_count: int = 0
    saved_album_count: int = 0
    followed_artist_count: int = 0
    entity_counts: dict[str, StatusCounts] = Field(default_factory=dict)
    playlists: list[PlaylistStatsView] = Field(default_factory=list)
    empty: bool = False
    message: str | None = None


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


def _migration_option(job: orm.MigrationJob, playlist_names: list[str]) -> MigrationOptionView:
    return MigrationOptionView(
        id=job.id,
        label=_migration_label(job, playlist_names),
        playlist_names=playlist_names,
        status=job.status,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        created_at=job.created_at,
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
    conditions = [orm.MigrationJob.user_id == user_id]
    if source_provider:
        conditions.append(orm.MigrationJob.source_provider == source_provider)
    if target_provider:
        conditions.append(orm.MigrationJob.target_provider == target_provider)
    return conditions


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
    jobs = list(
        (
            await session.execute(
                select(orm.MigrationJob)
                .where(orm.MigrationJob.user_id == user_id)
                .order_by(orm.MigrationJob.created_at.desc(), orm.MigrationJob.id.desc())
            )
        ).scalars()
    )
    names_by_job = await _playlist_names_by_job(session, jobs, user_id=user_id)
    return [_migration_option(job, names_by_job[job.id]) for job in jobs]


@router.post("", response_model=JobView)
async def create_migration(
    body: CreateMigration,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> JobView:
    if not body.selection.has_items():
        raise HTTPException(status_code=400, detail="Select at least one item to migrate")
    try:
        warnings, summary = await _validated_preflight(session, body, user_id=user_id)
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
            detail=MigrationWarningsView(warnings=warnings, summary=summary).model_dump(),
        )

    job = orm.MigrationJob(
        user_id=user_id,
        source_provider=body.source_provider,
        target_provider=body.target_provider,
        source_account_id=body.source_account_id,
        target_account_id=body.target_account_id,
        selection=body.selection.model_dump(),
        status="pending",
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
    if not body.selection.has_items():
        raise HTTPException(status_code=400, detail="Select at least one item to migrate")
    try:
        warnings, summary = await _validated_preflight(session, body, user_id=user_id)
        await session.commit()
        return MigrationWarningsView(warnings=warnings, summary=summary)
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


async def _validated_preflight(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> tuple[list[dict[str, str]], MigrationSelectionSummary]:
    source = get(body.source_provider)
    get(body.target_provider)

    source_caps = source.info.capabilities
    if body.selection.playlist_ids and not source_caps.can(Capability.READ_TRACKS):
        raise HTTPException(status_code=400, detail=f"{body.source_provider} cannot read tracks")
    await load_credential(
        session, account_id=body.source_account_id, provider=body.source_provider
    )
    await load_credential(
        session, account_id=body.target_account_id, provider=body.target_provider
    )
    return await _preflight(session, body, user_id=user_id)


async def _preflight(
    session: AsyncSession,
    body: CreateMigration,
    *,
    user_id: str,
) -> tuple[list[dict[str, str]], MigrationSelectionSummary]:
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
    _validate_target_capabilities(target, target_cred, selected, body.selection)
    await _validate_selected_library_entities(
        source,
        source_cred,
        body.selection,
    )
    total_tracks = sum(len(playlist.tracks) for playlist in selected.values())
    summary = MigrationSelectionSummary(
        playlists=len(body.selection.playlist_ids),
        tracks=total_tracks,
        saved_albums=len(body.selection.saved_album_ids),
        followed_artists=len(body.selection.followed_artist_ids),
    )
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
    warnings.extend(_artist_semantics_warnings(source, target, body.selection))
    return warnings, summary


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
    items = list(
        (
            await session.execute(
                select(orm.JobItem)
                .where(orm.JobItem.job_id == job_id)
                .order_by(orm.JobItem.source_playlist_id, orm.JobItem.position)
            )
        ).scalars()
    )
    names_by_job = await _playlist_names_by_job(session, [job], user_id=user_id)
    return _build_migration_stats(job, items, names_by_job[job.id])


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
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[JobItemView]:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    stmt = (
        select(orm.JobItem)
        .where(orm.JobItem.job_id == job_id)
        .order_by(orm.JobItem.source_playlist_id, orm.JobItem.position)
    )
    return [_item_view(item) for item in (await session.execute(stmt)).scalars()]


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

    if body.action == "skip":
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
    else:
        item.status = "failed"
        item.reason = (result.error if result else None) or "target rejected reviewed track"
    await commit_job_counts(session, job)
    return _item_view(item)


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
