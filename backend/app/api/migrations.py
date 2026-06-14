"""Migration jobs + live progress (phases 4-5).

Progress is delivered over SSE and derived from persisted ``job_item`` rows, so a
client that reconnects can resume via ``Last-Event-ID`` rather than losing state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import AuthExpired, NotFound, ProviderError, RateLimited
from app.core.capabilities import Capability
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


class JobView(BaseModel):
    id: str
    status: str
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


def _job_view(job: orm.MigrationJob) -> JobView:
    return JobView(
        id=job.id,
        status=job.status,
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
            redis.close(close_connection_pool=True)
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
        source = get(body.source_provider)
        target = get(body.target_provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    try:
        await load_credential(
            session, account_id=body.source_account_id, provider=body.source_provider
        )
        await load_credential(
            session, account_id=body.target_account_id, provider=body.target_provider
        )
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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
    if item.status not in {"needs_review", "failed"}:
        raise HTTPException(status_code=400, detail=f"item is already {item.status}")

    if body.action == "skip":
        item.status = "skipped"
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
        results = await target.add_tracks(target_cred, item.target_playlist_id, [target_uri])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
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
