from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    NotFound,
    ProviderAdapter,
    ProviderCredential,
    ProviderError,
    RateLimited,
    RemoveTracksResult,
    TrackRemoval,
)
from app.core.models import Playlist, PlaylistRef
from app.core.organizer import OrganizerAction, playlist_sequence_hash
from app.core.rate_limit import rate_limiter
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_sessionmaker
from app.db.repositories import invalidate_playlist_cache, load_fresh_credential
from app.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionOutcome:
    payload: dict


class ItemExecutionError(Exception):
    def __init__(self, message: str, *, payload: dict | None = None, retryable: bool = False):
        super().__init__(message)
        self.payload = payload or {}
        self.retryable = retryable


async def run_organizer(ctx: dict, job_id: str) -> None:
    logger.info("starting organizer job_id=%s", job_id)
    async with get_sessionmaker()() as session:
        job = await session.get(orm.OrganizerJob, job_id)
        if job is None:
            logger.error("organizer job_id=%s not found", job_id)
            return
        try:
            await _run(session, job)
        except asyncio.CancelledError:
            await session.rollback()
            await _mark_job_failed(session, job_id, "organizer cancelled or timed out")
            logger.exception("organizer job_id=%s cancelled or timed out", job_id)
            raise
        except Exception as exc:
            await session.rollback()
            await _mark_job_failed(session, job_id, str(exc))
            logger.exception("organizer job_id=%s failed", job_id)


async def _run(session: AsyncSession, job: orm.OrganizerJob) -> None:
    adapter = get(job.provider)
    credential, _ = await load_fresh_credential(
        session,
        account_id=job.account_id,
        adapter=adapter,
        provider=job.provider,
    )
    settings = get_settings()
    limiter_key = f"{job.provider}:{job.account_id}"
    rate_limiter.ensure_configured(
        limiter_key,
        capacity=settings.organizer_rate_limit_capacity,
        refill_per_s=settings.organizer_rate_limit_refill_per_s,
    )
    job.status = "running"
    job.error = None
    await session.commit()

    items = list((await session.execute(_items_for_execution_stmt(job.id))).scalars())
    had_success = False
    for item in items:
        succeeded = await _run_item(
            session,
            job,
            item,
            adapter=adapter,
            credential=credential,
            limiter_key=limiter_key,
        )
        had_success = had_success or succeeded

    if had_success:
        await invalidate_playlist_cache(
            session,
            user_id=job.user_id,
            provider=job.provider,
            account_id=job.account_id,
        )
    await _commit_counts(session, job)
    job.status = "partial" if job.failed else "done"
    await session.commit()


async def _run_item(
    session: AsyncSession,
    job: orm.OrganizerJob,
    item: orm.OrganizerItem,
    *,
    adapter: ProviderAdapter,
    credential: ProviderCredential,
    limiter_key: str,
) -> bool:
    settings = get_settings()
    item.status = "running"
    item.error = None
    await session.commit()

    for attempt in range(settings.organizer_retry_attempts):
        item.attempts = (item.attempts or 0) + 1
        await session.commit()
        try:
            await rate_limiter.acquire(
                limiter_key,
                cost=max(1, adapter.info.capabilities.write_quota_cost),
            )
            outcome = await _execute_item(adapter, credential, item)
        except RateLimited as exc:
            delay = exc.retry_after_s or min(2**attempt, settings.organizer_retry_max_delay_s)
            if (
                attempt + 1 < settings.organizer_retry_attempts
                and delay <= settings.organizer_retry_max_delay_s
            ):
                await asyncio.sleep(delay)
                continue
            await _fail_item(session, job, item, str(exc), retryable=True)
            return False
        except (AuthExpired, AccessDenied) as exc:
            await _fail_item(session, job, item, str(exc), retryable=True)
            return False
        except ItemExecutionError as exc:
            item.result_payload = exc.payload
            await _fail_item(session, job, item, str(exc), retryable=exc.retryable)
            return False
        except ProviderError as exc:
            await _fail_item(session, job, item, str(exc), retryable=False)
            return False

        item.status = "succeeded"
        item.retryable = False
        item.error = None
        item.result_payload = outcome.payload
        await _commit_counts(session, job)
        return True
    return False


async def _execute_item(
    adapter: ProviderAdapter,
    credential: ProviderCredential,
    item: orm.OrganizerItem,
) -> ExecutionOutcome:
    payload = item.request_payload or {}
    ref = PlaylistRef.model_validate(payload.get("playlist") or {})
    action = OrganizerAction(item.action)

    if action is OrganizerAction.UNFOLLOW_PLAYLIST:
        try:
            result = await adapter.unfollow_playlist(credential, ref)
        except NotFound:
            return ExecutionOutcome(payload={"already_absent": True})
        return ExecutionOutcome(payload=result.model_dump(mode="json"))

    if action is OrganizerAction.DELETE_PLAYLIST:
        current = await _listed_ref(adapter, credential, ref.id)
        if current is None:
            return ExecutionOutcome(payload={"already_absent": True})
        if current.is_owned is not True:
            detail = await adapter.read_playlist(credential, current)
            current = _ref_from_detail(detail, current)
        if current.is_owned is not True:
            raise AccessDenied(
                f"{adapter.info.display_name} playlist ownership is no longer confirmed"
            )
        try:
            result = await adapter.delete_playlist(credential, current)
        except NotFound:
            return ExecutionOutcome(payload={"already_absent": True})
        return ExecutionOutcome(payload=result.model_dump(mode="json"))

    removals = [TrackRemoval.model_validate(row) for row in payload.get("tracks") or []]
    try:
        current_playlist = await adapter.read_playlist(credential, ref)
    except NotFound:
        return ExecutionOutcome(payload={"already_absent": True})
    current_ref = _ref_from_detail(current_playlist, ref)
    if adapter.info.name == "spotify":
        current_hash = playlist_sequence_hash(current_playlist.tracks)
        if current_hash == payload.get("expected_sequence_hash"):
            return ExecutionOutcome(payload={"already_applied": True})
        if (
            current_hash != payload.get("baseline_sequence_hash")
            or current_ref.snapshot_id != ref.snapshot_id
        ):
            raise ItemExecutionError(
                "Spotify playlist changed after preflight; refresh and create a new organizer job"
            )
    elif adapter.info.name == "ytmusic":
        current_ids = {
            track.source_item_id for track in current_playlist.tracks if track.source_item_id
        }
        removals = [
            removal for removal in removals if removal.source_item_id in current_ids
        ]
        if not removals:
            return ExecutionOutcome(payload={"already_applied": True})
    result = await adapter.remove_tracks(credential, current_ref, removals)
    return _track_outcome(result)


def _track_outcome(result: RemoveTracksResult) -> ExecutionOutcome:
    payload = result.model_dump(mode="json")
    failed = [item for item in result.items if not item.ok]
    if failed:
        message = failed[0].error or "provider rejected a selected song removal"
        raise ItemExecutionError(message, payload=payload, retryable=False)
    return ExecutionOutcome(payload=payload)


async def _listed_ref(
    adapter: ProviderAdapter,
    credential: ProviderCredential,
    playlist_id: str,
) -> PlaylistRef | None:
    async for ref in adapter.iter_playlists(credential):
        if ref.id == playlist_id:
            return ref
    return None


def _ref_from_detail(detail: Playlist, fallback: PlaylistRef) -> PlaylistRef:
    return fallback.model_copy(
        update={
            "id": detail.id or fallback.id,
            "name": detail.name or fallback.name,
            "track_count": len(detail.tracks),
            "owner_id": detail.owner_id or fallback.owner_id,
            "owner_name": detail.owner_name or fallback.owner_name,
            "is_owned": detail.is_owned
            if detail.is_owned is not None
            else fallback.is_owned,
            "is_followed": detail.is_followed
            if detail.is_followed is not None
            else fallback.is_followed,
            "collaborative": detail.collaborative
            if detail.collaborative is not None
            else fallback.collaborative,
            "snapshot_id": detail.snapshot_id or fallback.snapshot_id,
            "created_at": detail.created_at or fallback.created_at,
            "updated_at": detail.updated_at or fallback.updated_at,
            "kind": detail.kind,
        }
    )


def _items_for_execution_stmt(job_id: str):
    return (
        select(orm.OrganizerItem)
        .where(
            orm.OrganizerItem.job_id == job_id,
            orm.OrganizerItem.status.not_in(["succeeded", "skipped", "failed"]),
        )
        .order_by(orm.OrganizerItem.created_at, orm.OrganizerItem.id)
    )


async def _fail_item(
    session: AsyncSession,
    job: orm.OrganizerJob,
    item: orm.OrganizerItem,
    error: str,
    *,
    retryable: bool,
) -> None:
    item.status = "failed"
    item.retryable = retryable
    item.error = error
    await _commit_counts(session, job)


async def _commit_counts(session: AsyncSession, job: orm.OrganizerJob) -> None:
    await session.flush()
    total = await session.scalar(
        select(func.count()).where(orm.OrganizerItem.job_id == job.id)
    )
    done = await session.scalar(
        select(func.count()).where(
            orm.OrganizerItem.job_id == job.id,
            orm.OrganizerItem.status.in_(["succeeded", "skipped"]),
        )
    )
    failed = await session.scalar(
        select(func.count()).where(
            orm.OrganizerItem.job_id == job.id,
            orm.OrganizerItem.status == "failed",
        )
    )
    job.total = int(total or 0)
    job.done = int(done or 0)
    job.failed = int(failed or 0)
    await session.commit()


async def _mark_job_failed(session: AsyncSession, job_id: str, error: str) -> None:
    job = await session.get(orm.OrganizerJob, job_id)
    if job is None:
        return
    job.status = "failed"
    job.error = error
    await session.commit()
