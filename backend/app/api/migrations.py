"""Migration jobs + live progress (phases 4-5).

Progress is delivered over SSE and derived from persisted ``job_item`` rows, so a
client that reconnects can resume via ``Last-Event-ID`` rather than losing state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
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

from app.core.adapter import AccessDenied, AuthExpired, NotFound, ProviderError, RateLimited
from app.core.capabilities import Capability
from app.core.migration_state import (
    has_track_overlap,
    keys_from_metadata,
    track_keys,
    track_selected,
    uri_keys,
)
from app.core.models import Playlist, PlaylistRef
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


@router.post("", response_model=JobView)
async def create_migration(
    body: CreateMigration,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = "local",
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
    )
    session.add(job)
    await session.commit()
    await _enqueue_or_inline(background_tasks, job.id)
    return _job_view(job)


@router.post("/preflight", response_model=MigrationWarningsView)
async def preflight_migration(
    body: CreateMigration,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = "local",
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
    target = get(body.target_provider)

    source_caps = source.info.capabilities
    target_caps = target.info.capabilities
    if not source_caps.can(Capability.READ_TRACKS):
        raise HTTPException(status_code=400, detail=f"{body.source_provider} cannot read tracks")
    if not (
        target_caps.can(Capability.CREATE_PLAYLIST) and target_caps.can(Capability.ADD_TRACKS)
    ):
        raise HTTPException(
            status_code=400, detail=f"{body.target_provider} cannot write playlists"
        )

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
        same_name = [ref for ref in target_refs if ref.name.strip() == source_playlist.name.strip()]
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


@router.get("/{job_id}", response_model=JobView)
async def get_migration(job_id: str) -> JobView:
    async with get_sessionmaker()() as session:
        job = await session.get(orm.MigrationJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="migration job not found")
        return _job_view(job)


@router.get("/{job_id}/items", response_model=list[JobItemView])
async def get_migration_items(job_id: str) -> list[JobItemView]:
    async with get_sessionmaker()() as session:
        job = await session.get(orm.MigrationJob, job_id)
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
) -> JobItemView:
    job = await session.get(orm.MigrationJob, job_id)
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
) -> list[JobItemView]:
    job = await session.get(orm.MigrationJob, job_id)
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


async def _progress_payload(job_id: str) -> dict:
    async with get_sessionmaker()() as session:
        job = await session.get(orm.MigrationJob, job_id)
        if job is None:
            return {"job_id": job_id, "missing": True}
        stmt = (
            select(orm.JobItem)
            .where(orm.JobItem.job_id == job_id)
            .order_by(orm.JobItem.source_playlist_id, orm.JobItem.position)
        )
        items = [_item_view(item).model_dump() for item in (await session.execute(stmt)).scalars()]
        return {"job": _job_view(job).model_dump(), "items": items}


async def _event_stream(job_id: str, request: Request) -> AsyncIterator[bytes]:
    event_id = 0
    while True:
        if await request.is_disconnected():
            break
        payload = await _progress_payload(job_id)
        yield f"id: {event_id}\nevent: progress\ndata: {json.dumps(payload)}\n\n".encode()
        job = payload.get("job")
        if isinstance(job, dict) and job.get("status") in {"done", "failed"}:
            break
        event_id += 1
        await asyncio.sleep(2)


@router.get("/{job_id}/events")
async def migration_events(job_id: str, request: Request) -> StreamingResponse:
    return StreamingResponse(_event_stream(job_id, request), media_type="text/event-stream")
