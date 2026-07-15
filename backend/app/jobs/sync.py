"""Persistent playlist synchronization orchestration for the ARQ worker."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.adapter import (
    AuthExpired,
    MirrorProviderAdapter,
    ProviderError,
    RateLimited,
    RefreshTokenExpired,
)
from app.core.models import Playlist, PlaylistKind, PlaylistRef, Track
from app.core.registry import get
from app.core.sync import (
    SyncMode,
    build_playlist_snapshot,
    carry_target_mappings,
    diff_snapshots,
    mirror_unavailable_reason,
    next_run_after,
    target_uri_sequence,
)
from app.db import models as orm
from app.db.base import get_sessionmaker
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential
from app.jobs.migration import run_migration
from app.settings import get_settings

logger = logging.getLogger(__name__)
ACTIVE_RUN_STATUSES = {"queued", "running"}
_SYNC_ENTRY_TOKEN = "ope_sync_entry_token"


class SyncAlreadyRunning(Exception):
    pass


@dataclass(frozen=True)
class SyncErrorPolicy:
    status: str
    pause_rule: bool
    retry_after_s: float | None


@dataclass(frozen=True)
class SyncJobOutcome:
    status: str
    review_count: int
    failed_count: int


class SyncPartialFailure(ProviderError):
    pass


async def create_queued_run(
    session: AsyncSession,
    *,
    rule_id: str,
    trigger: str,
    now: datetime,
) -> orm.SyncRun:
    rule = await session.scalar(
        select(orm.SyncRule).where(orm.SyncRule.id == rule_id).with_for_update()
    )
    if rule is None:
        raise KeyError(rule_id)
    active = await session.scalar(
        select(orm.SyncRun.id).where(
            orm.SyncRun.rule_id == rule_id,
            orm.SyncRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active is not None:
        raise SyncAlreadyRunning(rule_id)
    run_id = str(uuid.uuid4())
    run = orm.SyncRun(
        id=run_id,
        rule_id=rule.id,
        trigger=trigger,
        status="queued",
        lease_token=str(uuid.uuid4()),
        queue_job_id=f"sync-run:{run_id}",
    )
    session.add(run)
    rule.status = "queued"
    rule.last_run_at = now
    rule.next_run_at = None
    rule.last_error = None
    await session.flush()
    return run


async def recover_stale_runs(
    session: AsyncSession,
    *,
    now: datetime,
    stale_after_s: int,
    retry_delay_s: int,
) -> int:
    runs = list(
        (
            await session.execute(
                select(orm.SyncRun)
                .options(selectinload(orm.SyncRun.rule))
                .where(orm.SyncRun.status.in_(ACTIVE_RUN_STATUSES))
            )
        ).scalars()
    )
    cutoff = now - timedelta(seconds=stale_after_s)
    recovered = 0
    for run in runs:
        heartbeat = run.heartbeat_at or run.started_at or run.created_at
        if heartbeat is None or _aware(heartbeat) >= cutoff:
            continue
        checkpoint = await session.get(orm.SyncCheckpoint, run.rule_id)
        resolved_review = bool(
            checkpoint
            and checkpoint.unresolved
            and await _review_ready_for_run(session, run.id)
        )
        message = "sync worker stopped before the run completed"
        run.status = "review_required" if resolved_review else "failed"
        run.error = (
            "review was saved but finalization was interrupted"
            if resolved_review
            else message
        )
        run.finished_at = now
        run.lease_token = str(uuid.uuid4())
        if resolved_review:
            run.rule.status = "review_required"
            run.rule.last_error = run.error
            run.rule.next_run_at = None
        else:
            run.rule.status = "failed"
            run.rule.last_error = message
            run.rule.next_run_at = now + timedelta(seconds=retry_delay_s)
        recovered += 1
    if recovered:
        await session.commit()
    return recovered


def lease_is_current(run: orm.SyncRun, lease_token: str) -> bool:
    return run.status == "running" and run.lease_token == lease_token


def sync_error_policy(error: Exception) -> SyncErrorPolicy:
    if isinstance(error, SyncPartialFailure):
        return SyncErrorPolicy(
            status="partial_failure",
            pause_rule=False,
            retry_after_s=None,
        )
    if isinstance(
        error,
        (RefreshTokenExpired, AuthExpired, AccountNotFound, CredentialNotFound),
    ):
        return SyncErrorPolicy(
            status="reconnect_required",
            pause_rule=True,
            retry_after_s=None,
        )
    if isinstance(error, RateLimited):
        return SyncErrorPolicy(
            status="failed",
            pause_rule=False,
            retry_after_s=error.retry_after_s,
        )
    return SyncErrorPolicy(status="failed", pause_rule=False, retry_after_s=None)


def classify_job_items(items: list[orm.JobItem]) -> SyncJobOutcome:
    review_count = sum(
        item.status == "needs_review"
        or (item.status == "failed" and item.reason == "no target match found")
        for item in items
    )
    failed_count = sum(
        item.status == "failed" and item.reason != "no target match found" for item in items
    )
    if failed_count:
        status = "partial_failure"
    elif review_count:
        status = "review_required"
    else:
        status = "succeeded"
    return SyncJobOutcome(
        status=status,
        review_count=review_count,
        failed_count=failed_count,
    )


def review_finalization_ready(items: list[orm.JobItem]) -> bool:
    return all(item.status not in {"needs_review", "failed"} for item in items)


async def run_sync(ctx: dict, run_id: str) -> None:
    lease_token = await _start_run(run_id, expected_status="queued")
    if lease_token is None:
        return
    try:
        await _execute_sync(ctx, run_id=run_id, lease_token=lease_token)
    except asyncio.CancelledError:
        await _fail_run(run_id, lease_token, RuntimeError("sync cancelled or timed out"))
        raise
    except Exception as exc:
        await _fail_run(run_id, lease_token, exc)
        logger.exception("sync run_id=%s failed", run_id)


async def finalize_sync_review(ctx: dict, run_id: str) -> None:
    lease_token = await _start_run(run_id, expected_status="review_required")
    if lease_token is None:
        return
    try:
        await _finalize_reviewed_run(run_id=run_id, lease_token=lease_token)
    except Exception as exc:
        await _fail_run(run_id, lease_token, exc)
        logger.exception("sync review finalization run_id=%s failed", run_id)


async def schedule_syncs(ctx: dict) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        await recover_stale_runs(
            session,
            now=now,
            stale_after_s=settings.sync_stale_run_after_s,
            retry_delay_s=settings.sync_retry_delay_s,
        )
    queued = await _queued_runs()
    await _enqueue_runs(ctx, queued)
    async with get_sessionmaker()() as session:
        reviewed = await resolved_review_runs(session)
    await _enqueue_review_finalizers(ctx, reviewed)

    async with get_sessionmaker()() as session:
        rules = list(
            (
                await session.execute(
                    select(orm.SyncRule)
                    .where(
                        orm.SyncRule.enabled.is_(True),
                        orm.SyncRule.next_run_at.is_not(None),
                        orm.SyncRule.next_run_at <= now,
                        orm.SyncRule.status.not_in(["review_required", "reconnect_required"]),
                    )
                    .order_by(orm.SyncRule.next_run_at, orm.SyncRule.id)
                    .limit(settings.sync_scheduler_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        created: list[orm.SyncRun] = []
        for rule in rules:
            try:
                created.append(
                    await create_queued_run(
                        session,
                        rule_id=rule.id,
                        trigger="scheduled",
                        now=now,
                    )
                )
            except SyncAlreadyRunning:
                continue
        await session.commit()
    await _enqueue_runs(ctx, created)


async def _execute_sync(ctx: dict, *, run_id: str, lease_token: str) -> None:
    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id)
        checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
        if checkpoint is None:
            raise RuntimeError("sync checkpoint is missing")
        source = get(rule.source_provider)
        target = get(rule.target_provider)
        mode = SyncMode(rule.mode)
        if mode is SyncMode.MIRROR:
            kind = PlaylistKind((checkpoint.source_snapshot or {}).get("kind", "standard"))
            reason = mirror_unavailable_reason(target, kind=kind)
            if reason:
                raise ProviderError(reason)
        source_cred, _ = await load_fresh_credential(
            session,
            account_id=rule.source_account_id,
            adapter=source,
            provider=rule.source_provider,
        )
        target_cred, _ = await load_fresh_credential(
            session,
            account_id=rule.target_account_id,
            adapter=target,
            provider=rule.target_provider,
        )
        source_playlist = await source.read_playlist(
            source_cred,
            PlaylistRef(id=rule.source_playlist_id, name=rule.source_playlist_name),
        )
        target_playlist = await target.read_playlist(
            target_cred,
            PlaylistRef(id=rule.target_playlist_id, name=rule.target_playlist_name),
        )
        source_snapshot = build_playlist_snapshot(
            source_playlist, provider=rule.source_provider
        )
        target_before = build_playlist_snapshot(
            target_playlist, provider=rule.target_provider
        )
        diff = diff_snapshots(checkpoint.source_snapshot or {}, source_snapshot)
        mappings = carry_target_mappings(
            checkpoint.source_snapshot or {},
            source_snapshot,
            dict(checkpoint.mappings or {}),
        )
        run.source_snapshot = source_snapshot
        run.target_before = target_before
        run.added = len(diff.added_positions)
        run.removed = diff.removed_count
        run.reordered = diff.reordered_count
        run.heartbeat_at = datetime.now(UTC)
        job = _sync_migration_job(
            run=run,
            rule=rule,
            source_playlist=source_playlist,
            source_snapshot=source_snapshot,
            added_positions=diff.added_positions,
            match_only=mode is SyncMode.MIRROR,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    if diff.added_positions:
        await run_migration(ctx, job_id, propagate=True)

    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id)
        checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
        job = await session.scalar(
            select(orm.MigrationJob).where(orm.MigrationJob.sync_run_id == run.id)
        )
        if checkpoint is None or job is None:
            raise RuntimeError("sync state disappeared during execution")
        if not diff.added_positions:
            job.status = "done"
        items = list(
            (
                await session.execute(
                    select(orm.JobItem)
                    .where(orm.JobItem.job_id == job.id)
                    .order_by(orm.JobItem.position, orm.JobItem.id)
                )
            ).scalars()
        )
        outcome = classify_job_items(items)
        mappings.update(_job_item_mappings(items))
        if outcome.status == "partial_failure":
            raise SyncPartialFailure(
                f"{outcome.failed_count} track writes failed; retry is scheduled"
            )
        if outcome.status == "review_required":
            checkpoint.source_snapshot = run.source_snapshot
            checkpoint.target_snapshot = run.target_before
            checkpoint.mappings = mappings
            checkpoint.unresolved = [
                item.id for item in items if item.status in {"needs_review", "failed"}
            ]
            message = f"{outcome.review_count} tracks require review"
            await _finish_attention(session, run, rule, lease_token, message)
            return
        await session.commit()

    target_after = target_before
    if mode is SyncMode.MIRROR:
        target_after = await _apply_mirror(
            run_id=run_id,
            lease_token=lease_token,
            target=target,
            target_cred=target_cred,
            target_before=target_before,
            mappings=mappings,
            job_id=job_id,
        )
    else:
        target_after_playlist = await target.read_playlist(
            target_cred,
            PlaylistRef(id=rule.target_playlist_id, name=rule.target_playlist_name),
        )
        target_after = build_playlist_snapshot(
            target_after_playlist, provider=rule.target_provider
        )
    await _complete_run(
        run_id=run_id,
        lease_token=lease_token,
        source_snapshot=source_snapshot,
        target_snapshot=target_after,
        mappings=mappings,
    )


async def _finalize_reviewed_run(*, run_id: str, lease_token: str) -> None:
    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id)
        checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
        job = await session.scalar(
            select(orm.MigrationJob).where(orm.MigrationJob.sync_run_id == run.id)
        )
        if checkpoint is None or job is None:
            raise RuntimeError("sync review state is missing")
        items = list(
            (
                await session.execute(
                    select(orm.JobItem)
                    .where(orm.JobItem.job_id == job.id)
                    .order_by(orm.JobItem.position, orm.JobItem.id)
                )
            ).scalars()
        )
        if not review_finalization_ready(items):
            raise RuntimeError("sync review still has unresolved tracks")
        mappings = dict(checkpoint.mappings or {})
        mappings.update(_job_item_mappings(items))
        checkpoint.mappings = mappings
        checkpoint.unresolved = []
        await session.commit()
        target = get(rule.target_provider)
        target_cred, _ = await load_fresh_credential(
            session,
            account_id=rule.target_account_id,
            adapter=target,
            provider=rule.target_provider,
        )
        target_playlist = await target.read_playlist(
            target_cred,
            PlaylistRef(id=rule.target_playlist_id, name=rule.target_playlist_name),
        )
        target_before = build_playlist_snapshot(
            target_playlist, provider=rule.target_provider
        )
        mode = SyncMode(rule.mode)
        job_id = job.id
        source_snapshot = dict(run.source_snapshot or checkpoint.source_snapshot or {})

    if mode is SyncMode.MIRROR:
        target_after = await _apply_mirror(
            run_id=run_id,
            lease_token=lease_token,
            target=target,
            target_cred=target_cred,
            target_before=target_before,
            mappings=mappings,
            job_id=job_id,
        )
    else:
        target_after = target_before
    await _complete_run(
        run_id=run_id,
        lease_token=lease_token,
        source_snapshot=source_snapshot,
        target_snapshot=target_after,
        mappings=mappings,
    )


async def _apply_mirror(
    *,
    run_id: str,
    lease_token: str,
    target: object,
    target_cred,
    target_before: dict,
    mappings: dict[str, str | None],
    job_id: str,
) -> dict:
    reason = mirror_unavailable_reason(target)
    if reason or not isinstance(target, MirrorProviderAdapter):
        raise ProviderError(reason or "target cannot mirror playlists")
    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id)
        if not lease_is_current(run, lease_token):
            raise RuntimeError("sync lease expired before mirror write")
        entries = list((run.source_snapshot or {}).get("tracks") or [])
        missing = [entry["token"] for entry in entries if entry["token"] not in mappings]
        if missing:
            raise RuntimeError(f"sync is missing {len(missing)} target track mappings")
        desired = [
            uri
            for entry in entries
            if (uri := mappings.get(entry["token"])) is not None
        ]
        previous = target_uri_sequence(target_before)
        if previous == desired:
            return target_before
        ledger = orm.OperationLedger(
            job_id=job_id,
            op="replace_playlist_tracks",
            intent={
                "playlist_id": rule.target_playlist_id,
                "uris": desired,
                "previous_uris": previous,
            },
            observed_target_id=rule.target_playlist_id,
            state="intended",
        )
        session.add(ledger)
        await session.commit()
        try:
            await target.replace_playlist_tracks(
                target_cred, rule.target_playlist_id, desired
            )
        except ProviderError as exc:
            ledger = await session.get(orm.OperationLedger, ledger.id)
            if ledger is not None:
                ledger.state = "ambiguous"
                await session.commit()
            restored = False
            if len(previous) == len(target_before.get("tracks") or []):
                try:
                    await target.replace_playlist_tracks(
                        target_cred, rule.target_playlist_id, previous
                    )
                    restored = True
                except ProviderError:
                    logger.exception(
                        "mirror rollback failed run_id=%s playlist_id=%s",
                        run_id,
                        rule.target_playlist_id,
                    )
            suffix = "target restored" if restored else "target may be partially replaced"
            if isinstance(exc, (AuthExpired, RateLimited)):
                raise
            raise ProviderError(f"{exc}; {suffix}") from exc
        ledger = await session.get(orm.OperationLedger, ledger.id)
        if ledger is not None:
            ledger.state = "done"
            await session.commit()
        final_playlist = await target.read_playlist(
            target_cred,
            PlaylistRef(id=rule.target_playlist_id, name=rule.target_playlist_name),
        )
        final_snapshot = build_playlist_snapshot(
            final_playlist, provider=rule.target_provider
        )
        if target_uri_sequence(final_snapshot) != desired:
            if ledger is not None:
                ledger.state = "ambiguous"
                await session.commit()
            raise ProviderError("target playlist did not match the requested mirror order")
        return final_snapshot


async def _complete_run(
    *,
    run_id: str,
    lease_token: str,
    source_snapshot: dict,
    target_snapshot: dict,
    mappings: dict[str, str | None],
) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id)
        if not lease_is_current(run, lease_token):
            logger.warning("ignoring stale sync completion run_id=%s", run_id)
            return
        checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
        if checkpoint is None:
            raise RuntimeError("sync checkpoint is missing")
        checkpoint.source_snapshot = source_snapshot
        checkpoint.target_snapshot = target_snapshot
        checkpoint.mappings = mappings
        checkpoint.unresolved = []
        run.status = "succeeded"
        run.target_after = target_snapshot
        run.error = None
        run.finished_at = now
        run.heartbeat_at = now
        rule.status = "succeeded" if rule.enabled else "paused"
        rule.last_success_at = now
        rule.last_error = None
        rule.last_added = run.added
        rule.last_removed = run.removed
        rule.last_reordered = run.reordered
        rule.next_run_at = (
            next_run_after(now, cadence_minutes=rule.cadence_minutes)
            if rule.enabled
            else None
        )
        await session.commit()


async def _finish_attention(
    session: AsyncSession,
    run: orm.SyncRun,
    rule: orm.SyncRule,
    lease_token: str,
    message: str,
) -> None:
    if not lease_is_current(run, lease_token):
        return
    now = datetime.now(UTC)
    run.status = "review_required"
    run.error = message
    run.finished_at = now
    run.heartbeat_at = now
    rule.status = "review_required"
    rule.last_error = message
    rule.last_added = run.added
    rule.last_removed = run.removed
    rule.last_reordered = run.reordered
    rule.next_run_at = None
    await session.commit()


async def _fail_run(run_id: str, lease_token: str, error: Exception) -> None:
    policy = sync_error_policy(error)
    now = datetime.now(UTC)
    settings = get_settings()
    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id)
        if not lease_is_current(run, lease_token):
            return
        message = str(error) or error.__class__.__name__
        run.status = policy.status
        run.error = message
        run.finished_at = now
        run.heartbeat_at = now
        rule.status = policy.status
        rule.last_error = message
        rule.last_added = run.added
        rule.last_removed = run.removed
        rule.last_reordered = run.reordered
        if policy.pause_rule:
            rule.enabled = False
            rule.next_run_at = None
        else:
            retry_after = policy.retry_after_s or settings.sync_retry_delay_s
            rule.next_run_at = now + timedelta(seconds=retry_after)
        await session.commit()


async def _start_run(run_id: str, *, expected_status: str) -> str | None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        run, rule = await _run_and_rule(session, run_id, for_update=True)
        if run.status != expected_status:
            return None
        lease_token = str(uuid.uuid4())
        run.status = "running"
        run.lease_token = lease_token
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        run.error = None
        rule.status = "running"
        rule.last_error = None
        await session.commit()
        return lease_token


async def _run_and_rule(
    session: AsyncSession,
    run_id: str,
    *,
    for_update: bool = False,
) -> tuple[orm.SyncRun, orm.SyncRule]:
    stmt = (
        select(orm.SyncRun)
        .options(selectinload(orm.SyncRun.rule))
        .where(orm.SyncRun.id == run_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    run = await session.scalar(stmt)
    if run is None:
        raise KeyError(run_id)
    return run, run.rule


def _sync_migration_job(
    *,
    run: orm.SyncRun,
    rule: orm.SyncRule,
    source_playlist: Playlist,
    source_snapshot: dict,
    added_positions: list[int],
    match_only: bool,
) -> orm.MigrationJob:
    selected_entries = [
        entry
        for entry in source_snapshot.get("tracks") or []
        if int(entry["position"]) in set(added_positions)
    ]
    tracks: list[Track] = []
    for entry in selected_entries:
        track = Track.model_validate(entry["track"])
        metadata = dict(track.metadata or {})
        metadata[_SYNC_ENTRY_TOKEN] = entry["token"]
        tracks.append(track.model_copy(update={"metadata": metadata}))
    playlist_snapshot = source_playlist.model_copy(update={"tracks": tracks})
    return orm.MigrationJob(
        user_id=rule.user_id,
        source_provider=rule.source_provider,
        target_provider=rule.target_provider,
        source_account_id=rule.source_account_id,
        target_account_id=rule.target_account_id,
        selection={
            "playlist_ids": [rule.source_playlist_id],
            "playlist_snapshots": {
                rule.source_playlist_id: playlist_snapshot.model_dump(mode="json")
            },
            "target_playlist_ids": {
                rule.source_playlist_id: rule.target_playlist_id
            },
            "match_only": match_only,
        },
        status="pending" if tracks else "done",
        origin="sync",
        sync_run_id=run.id,
    )


def _job_item_mappings(items: list[orm.JobItem]) -> dict[str, str | None]:
    mappings: dict[str, str | None] = {}
    for item in items:
        metadata = item.source_metadata or {}
        track_metadata = metadata.get("metadata")
        token = (
            track_metadata.get(_SYNC_ENTRY_TOKEN)
            if isinstance(track_metadata, dict)
            else None
        )
        if not isinstance(token, str) or not token:
            continue
        if item.status in {"written", "matched"} and item.target_uri:
            mappings[token] = item.target_uri
        elif item.status == "skipped":
            mappings[token] = item.target_uri
    return mappings


async def _queued_runs() -> list[orm.SyncRun]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(orm.SyncRun)
                    .where(orm.SyncRun.status == "queued")
                    .order_by(orm.SyncRun.created_at, orm.SyncRun.id)
                )
            ).scalars()
        )


async def resolved_review_runs(session: AsyncSession) -> list[orm.SyncRun]:
    runs = list(
        (
            await session.execute(
                select(orm.SyncRun)
                .join(
                    orm.MigrationJob,
                    orm.MigrationJob.sync_run_id == orm.SyncRun.id,
                )
                .where(orm.SyncRun.status == "review_required")
                .order_by(orm.SyncRun.created_at, orm.SyncRun.id)
            )
        ).scalars()
    )
    ready: list[orm.SyncRun] = []
    for run in runs:
        if await _review_ready_for_run(session, run.id):
            ready.append(run)
    return ready


async def _review_ready_for_run(session: AsyncSession, run_id: str) -> bool:
    job_id = await session.scalar(
        select(orm.MigrationJob.id).where(orm.MigrationJob.sync_run_id == run_id)
    )
    if job_id is None:
        return False
    unresolved = await session.scalar(
        select(orm.JobItem.id)
        .where(
            orm.JobItem.job_id == job_id,
            orm.JobItem.status.in_(["needs_review", "failed"]),
        )
        .limit(1)
    )
    return unresolved is None


async def _enqueue_runs(ctx: dict, runs: list[orm.SyncRun]) -> None:
    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("ARQ worker context is missing Redis")
    for run in runs:
        await redis.enqueue_job(
            "run_sync",
            run.id,
            _job_id=run.queue_job_id,
        )


async def _enqueue_review_finalizers(ctx: dict, runs: list[orm.SyncRun]) -> None:
    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("ARQ worker context is missing Redis")
    for run in runs:
        await redis.enqueue_job(
            "finalize_sync_review",
            run.id,
            _job_id=f"sync-review:{run.id}",
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
