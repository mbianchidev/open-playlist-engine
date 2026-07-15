"""Persistent scheduled playlist synchronization rules and controls."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.core.adapter import AccessDenied, AuthExpired, NotFound, ProviderError, RateLimited
from app.core.models import Playlist, PlaylistKind, PlaylistRef, Track
from app.core.registry import get
from app.core.sync import (
    SyncMode,
    build_playlist_snapshot,
    mirror_unavailable_reason,
    next_run_after,
)
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    load_fresh_credential,
)
from app.jobs.sync import SyncAlreadyRunning, create_queued_run, run_sync
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/syncs", tags=["syncs"])


class CreateSyncRule(BaseModel):
    migration_job_id: str
    mode: Literal["add_only", "mirror"] = "add_only"
    cadence_minutes: int = Field(default=60, ge=1)
    timezone: str = "UTC"


class UpdateSyncRule(BaseModel):
    mode: Literal["add_only", "mirror"] | None = None
    cadence_minutes: int | None = Field(default=None, ge=1)
    timezone: str | None = None


class SyncRunView(BaseModel):
    id: str
    trigger: str
    status: str
    migration_job_id: str | None = None
    added: int = 0
    removed: int = 0
    reordered: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


class SyncRuleView(BaseModel):
    id: str
    source_provider: str
    source_account_id: str
    source_playlist_id: str
    source_playlist_name: str
    target_provider: str
    target_account_id: str
    target_playlist_id: str
    target_playlist_name: str
    mode: str
    cadence_minutes: int
    timezone: str
    enabled: bool
    status: str
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None
    last_added: int = 0
    last_removed: int = 0
    last_reordered: int = 0
    latest_run: SyncRunView | None = None


@router.get("", response_model=list[SyncRuleView])
async def list_syncs(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[SyncRuleView]:
    rules = list(
        (
            await session.execute(
                select(orm.SyncRule)
                .where(orm.SyncRule.user_id == user_id)
                .order_by(orm.SyncRule.created_at.desc(), orm.SyncRule.id.desc())
            )
        ).scalars()
    )
    return await _rule_views(session, rules)


@router.post("", response_model=SyncRuleView)
async def create_sync(
    body: CreateSyncRule,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SyncRuleView:
    _validate_schedule(body.cadence_minutes, body.timezone)
    job = await session.scalar(
        select(orm.MigrationJob).where(
            orm.MigrationJob.id == body.migration_job_id,
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.origin == "manual",
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="completed migration not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail="migration must finish before creating a sync")
    playlist_id = _full_playlist_id(job)
    items = list(
        (
            await session.execute(
                select(orm.JobItem)
                .where(
                    orm.JobItem.job_id == job.id,
                    orm.JobItem.source_playlist_id == playlist_id,
                )
                .order_by(orm.JobItem.position, orm.JobItem.id)
            )
        ).scalars()
    )
    unresolved = [item for item in items if item.status in {"failed", "needs_review"}]
    if unresolved:
        raise HTTPException(
            status_code=409,
            detail="resolve failed and review-required migration tracks before creating a sync",
        )
    target_playlist_id = await _target_playlist_id(session, job, items)
    _ensure_distinct_endpoints(
        job.source_provider,
        job.source_account_id,
        playlist_id,
        job.target_provider,
        job.target_account_id,
        target_playlist_id,
    )
    await _lock_rule_graph(session, user_id)
    await _ensure_no_feedback_loop(
        session,
        user_id=user_id,
        source_provider=job.source_provider,
        source_account_id=job.source_account_id,
        source_playlist_id=playlist_id,
        target_provider=job.target_provider,
        target_account_id=job.target_account_id,
        target_playlist_id=target_playlist_id,
    )

    try:
        source = get(job.source_provider)
        target = get(job.target_provider)
        source_cred, _ = await load_fresh_credential(
            session,
            account_id=job.source_account_id,
            adapter=source,
            provider=job.source_provider,
        )
        target_cred, _ = await load_fresh_credential(
            session,
            account_id=job.target_account_id,
            adapter=target,
            provider=job.target_provider,
        )
        source_playlist = await source.read_playlist(
            source_cred, PlaylistRef(id=playlist_id, name=playlist_id)
        )
        target_playlist = await target.read_playlist(
            target_cred,
            PlaylistRef(id=target_playlist_id, name=target_playlist_id),
        )
        if SyncMode(body.mode) is SyncMode.MIRROR:
            _ensure_mirror_available(target, source_playlist.kind)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound, NotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_snapshot = build_playlist_snapshot(
        source_playlist, provider=job.source_provider
    )
    migration_snapshot = _migration_source_snapshot(
        playlist_id=playlist_id,
        playlist_name=source_playlist.name,
        provider=job.source_provider,
        items=items,
    )
    if _snapshot_tokens(migration_snapshot) != _snapshot_tokens(source_snapshot):
        raise HTTPException(
            status_code=409,
            detail="source playlist changed since the migration; migrate it again first",
        )
    target_snapshot = build_playlist_snapshot(
        target_playlist, provider=job.target_provider
    )
    mappings = _initial_mappings(migration_snapshot, items)
    if SyncMode(body.mode) is SyncMode.MIRROR:
        missing = [
            entry["token"]
            for entry in source_snapshot["tracks"]
            if not mappings.get(entry["token"])
        ]
        if missing:
            raise HTTPException(
                status_code=409,
                detail="mirror requires a target match for every migratable source track",
            )

    now = datetime.now(UTC)
    rule = orm.SyncRule(
        user_id=user_id,
        source_provider=job.source_provider,
        source_account_id=job.source_account_id,
        source_playlist_id=playlist_id,
        source_playlist_name=source_playlist.name,
        target_provider=job.target_provider,
        target_account_id=job.target_account_id,
        target_playlist_id=target_playlist_id,
        target_playlist_name=target_playlist.name,
        mode=body.mode,
        cadence_minutes=body.cadence_minutes,
        timezone=body.timezone,
        enabled=True,
        status="idle",
        next_run_at=next_run_after(now, cadence_minutes=body.cadence_minutes),
    )
    session.add(rule)
    await session.flush()
    session.add(
        orm.SyncCheckpoint(
            rule_id=rule.id,
            source_snapshot=source_snapshot,
            target_snapshot=target_snapshot,
            mappings=mappings,
            unresolved=[],
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="a sync rule already exists for these playlist endpoints",
        ) from exc
    return _rule_view(rule)


@router.get("/{rule_id}", response_model=SyncRuleView)
async def get_sync(
    rule_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SyncRuleView:
    rule = await _owned_rule(session, rule_id, user_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="sync rule not found")
    return (await _rule_views(session, [rule]))[0]


@router.patch("/{rule_id}", response_model=SyncRuleView)
async def update_sync(
    rule_id: str,
    body: UpdateSyncRule,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SyncRuleView:
    rule = await _owned_rule(session, rule_id, user_id, for_update=True)
    if rule is None:
        raise HTTPException(status_code=404, detail="sync rule not found")
    await _ensure_not_active(session, rule.id)
    cadence = body.cadence_minutes or rule.cadence_minutes
    timezone = body.timezone or rule.timezone
    _validate_schedule(cadence, timezone)
    if body.mode == SyncMode.MIRROR:
        target = get(rule.target_provider)
        checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
        kind = _checkpoint_kind(checkpoint)
        _ensure_mirror_available(target, kind)
        missing = [
            entry["token"]
            for entry in (checkpoint.source_snapshot.get("tracks") if checkpoint else [])
            if not (checkpoint.mappings if checkpoint else {}).get(entry["token"])
        ]
        if missing:
            raise HTTPException(
                status_code=409,
                detail="mirror requires a target match for every migratable source track",
            )
    if body.mode is not None:
        rule.mode = body.mode
    rule.cadence_minutes = cadence
    rule.timezone = timezone
    if rule.enabled:
        rule.next_run_at = next_run_after(datetime.now(UTC), cadence_minutes=cadence)
    await session.commit()
    return (await _rule_views(session, [rule]))[0]


@router.post("/{rule_id}/pause", response_model=SyncRuleView)
async def pause_sync(
    rule_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SyncRuleView:
    rule = await _owned_rule(session, rule_id, user_id, for_update=True)
    if rule is None:
        raise HTTPException(status_code=404, detail="sync rule not found")
    checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
    rule.enabled = False
    rule.status = "review_required" if checkpoint and checkpoint.unresolved else "paused"
    rule.next_run_at = None
    await session.commit()
    return (await _rule_views(session, [rule]))[0]


@router.post("/{rule_id}/resume", response_model=SyncRuleView)
async def resume_sync(
    rule_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SyncRuleView:
    rule = await _owned_rule(session, rule_id, user_id, for_update=True)
    if rule is None:
        raise HTTPException(status_code=404, detail="sync rule not found")
    await _ensure_not_active(session, rule.id)
    await _ensure_no_unresolved_review(session, rule.id)
    await _lock_rule_graph(session, user_id)
    await _ensure_no_feedback_loop(
        session,
        user_id=user_id,
        source_provider=rule.source_provider,
        source_account_id=rule.source_account_id,
        source_playlist_id=rule.source_playlist_id,
        target_provider=rule.target_provider,
        target_account_id=rule.target_account_id,
        target_playlist_id=rule.target_playlist_id,
        exclude_rule_id=rule.id,
    )
    if SyncMode(rule.mode) is SyncMode.MIRROR:
        checkpoint = await session.get(orm.SyncCheckpoint, rule.id)
        _ensure_mirror_available(get(rule.target_provider), _checkpoint_kind(checkpoint))
    rule.enabled = True
    rule.status = "idle"
    rule.last_error = None
    rule.next_run_at = next_run_after(
        datetime.now(UTC), cadence_minutes=rule.cadence_minutes
    )
    await session.commit()
    return (await _rule_views(session, [rule]))[0]


@router.post("/{rule_id}/run", response_model=SyncRunView)
async def run_sync_now(
    rule_id: str,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SyncRunView:
    rule = await _owned_rule(session, rule_id, user_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="sync rule not found")
    await _ensure_no_unresolved_review(session, rule.id)
    try:
        run = await create_queued_run(
            session,
            rule_id=rule.id,
            trigger="manual",
            now=datetime.now(UTC),
        )
        await session.commit()
    except SyncAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail="sync rule is already running") from exc
    await _enqueue_or_inline(background_tasks, run)
    return _run_view(run)


@router.delete("/{rule_id}", status_code=204)
async def delete_sync(
    rule_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> Response:
    rule = await _owned_rule(session, rule_id, user_id, for_update=True)
    if rule is None:
        raise HTTPException(status_code=404, detail="sync rule not found")
    await _ensure_not_active(session, rule.id)
    await session.delete(rule)
    await session.commit()
    return Response(status_code=204)


async def _rule_views(
    session: AsyncSession, rules: list[orm.SyncRule]
) -> list[SyncRuleView]:
    if not rules:
        return []
    runs = list(
        (
            await session.execute(
                select(orm.SyncRun)
                .where(orm.SyncRun.rule_id.in_([rule.id for rule in rules]))
                .order_by(orm.SyncRun.created_at.desc(), orm.SyncRun.id.desc())
            )
        ).scalars()
    )
    latest: dict[str, orm.SyncRun] = {}
    for run in runs:
        latest.setdefault(run.rule_id, run)
    job_ids: dict[str, str] = {}
    if latest:
        rows = await session.execute(
            select(orm.MigrationJob.sync_run_id, orm.MigrationJob.id).where(
                orm.MigrationJob.sync_run_id.in_(
                    latest_run.id for latest_run in latest.values()
                )
            )
        )
        job_ids = {
            run_id: job_id
            for run_id, job_id in rows.all()
            if run_id is not None
        }
    views: list[SyncRuleView] = []
    for rule in rules:
        latest_run = latest.get(rule.id)
        migration_job_id = job_ids.get(latest_run.id) if latest_run else None
        views.append(_rule_view(rule, latest_run, migration_job_id))
    return views


def _rule_view(
    rule: orm.SyncRule,
    latest_run: orm.SyncRun | None = None,
    migration_job_id: str | None = None,
) -> SyncRuleView:
    return SyncRuleView(
        id=rule.id,
        source_provider=rule.source_provider,
        source_account_id=rule.source_account_id,
        source_playlist_id=rule.source_playlist_id,
        source_playlist_name=rule.source_playlist_name,
        target_provider=rule.target_provider,
        target_account_id=rule.target_account_id,
        target_playlist_id=rule.target_playlist_id,
        target_playlist_name=rule.target_playlist_name,
        mode=rule.mode,
        cadence_minutes=rule.cadence_minutes,
        timezone=rule.timezone,
        enabled=rule.enabled,
        status=rule.status,
        last_run_at=rule.last_run_at,
        last_success_at=rule.last_success_at,
        next_run_at=rule.next_run_at,
        last_error=rule.last_error,
        last_added=rule.last_added,
        last_removed=rule.last_removed,
        last_reordered=rule.last_reordered,
        latest_run=(
            _run_view(latest_run, migration_job_id=migration_job_id)
            if latest_run
            else None
        ),
    )


def _run_view(
    run: orm.SyncRun,
    *,
    migration_job_id: str | None = None,
) -> SyncRunView:
    return SyncRunView(
        id=run.id,
        trigger=run.trigger,
        status=run.status,
        migration_job_id=migration_job_id,
        added=run.added,
        removed=run.removed,
        reordered=run.reordered,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
    )


def _full_playlist_id(job: orm.MigrationJob) -> str:
    selection = job.selection or {}
    playlist_ids = list(selection.get("playlist_ids") or [])
    if len(playlist_ids) != 1:
        raise HTTPException(
            status_code=409,
            detail="sync rules require a completed single-playlist migration",
        )
    playlist_id = str(playlist_ids[0])
    selected_tracks = (selection.get("tracks") or {}).get(playlist_id) or []
    if selected_tracks:
        raise HTTPException(
            status_code=409,
            detail="sync rules require a completed full-playlist migration",
        )
    return playlist_id


async def _target_playlist_id(
    session: AsyncSession,
    job: orm.MigrationJob,
    items: list[orm.JobItem],
) -> str:
    target_ids = {item.target_playlist_id for item in items if item.target_playlist_id}
    if not target_ids:
        ledger_id = await session.scalar(
            select(orm.OperationLedger.observed_target_id)
            .where(
                orm.OperationLedger.job_id == job.id,
                orm.OperationLedger.op == "create_playlist",
                orm.OperationLedger.observed_target_id.is_not(None),
            )
            .order_by(orm.OperationLedger.updated_at.desc())
            .limit(1)
        )
        if ledger_id:
            target_ids.add(ledger_id)
    if len(target_ids) != 1:
        raise HTTPException(
            status_code=409,
            detail="completed migration does not identify exactly one target playlist",
        )
    return next(iter(target_ids))


def _initial_mappings(
    source_snapshot: dict,
    items: list[orm.JobItem],
) -> dict[str, str | None]:
    by_position: dict[int, str | None] = {}
    for item in items:
        if item.status in {"written", "skipped"}:
            by_position.setdefault(item.position, item.target_uri)
    return {
        entry["token"]: by_position[int(entry["position"])]
        for entry in source_snapshot.get("tracks") or []
        if int(entry["position"]) in by_position
    }


def _migration_source_snapshot(
    *,
    playlist_id: str,
    playlist_name: str,
    provider: str,
    items: list[orm.JobItem],
) -> dict:
    tracks: list[Track] = []
    for item in items:
        try:
            tracks.append(Track.model_validate(item.source_metadata))
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail="migration does not contain a reusable source snapshot",
            ) from exc
    return build_playlist_snapshot(
        Playlist(id=playlist_id, name=playlist_name, tracks=tracks),
        provider=provider,
    )


def _snapshot_tokens(snapshot: dict) -> list[str]:
    return [str(entry["token"]) for entry in snapshot.get("tracks") or []]


def _validate_schedule(cadence_minutes: int, timezone: str) -> None:
    settings = get_settings()
    if not (
        settings.sync_min_cadence_minutes
        <= cadence_minutes
        <= settings.sync_max_cadence_minutes
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "cadence_minutes must be between "
                f"{settings.sync_min_cadence_minutes} and "
                f"{settings.sync_max_cadence_minutes}"
            ),
        )
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=400, detail="timezone must be a valid IANA name") from exc


def _ensure_mirror_available(target: object, kind: PlaylistKind) -> None:
    reason = mirror_unavailable_reason(target, kind=kind)
    if reason:
        raise HTTPException(status_code=400, detail=reason)


def _checkpoint_kind(checkpoint: orm.SyncCheckpoint | None) -> PlaylistKind:
    if checkpoint is None:
        return PlaylistKind.STANDARD
    return PlaylistKind((checkpoint.source_snapshot or {}).get("kind", "standard"))


def _ensure_distinct_endpoints(
    source_provider: str,
    source_account_id: str,
    source_playlist_id: str,
    target_provider: str,
    target_account_id: str,
    target_playlist_id: str,
) -> None:
    if (
        source_provider,
        source_account_id,
        source_playlist_id,
    ) == (
        target_provider,
        target_account_id,
        target_playlist_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="a sync source and target cannot be the same playlist",
        )


async def _ensure_no_feedback_loop(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    source_playlist_id: str,
    target_provider: str,
    target_account_id: str,
    target_playlist_id: str,
    exclude_rule_id: str | None = None,
) -> None:
    stmt = select(
        orm.SyncRule.id,
        orm.SyncRule.source_provider,
        orm.SyncRule.source_account_id,
        orm.SyncRule.source_playlist_id,
        orm.SyncRule.target_provider,
        orm.SyncRule.target_account_id,
        orm.SyncRule.target_playlist_id,
    ).where(orm.SyncRule.user_id == user_id)
    if exclude_rule_id:
        stmt = stmt.where(orm.SyncRule.id != exclude_rule_id)
    rows = (await session.execute(stmt)).all()
    adjacency: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for row in rows:
        source = (row.source_provider, row.source_account_id, row.source_playlist_id)
        target = (row.target_provider, row.target_account_id, row.target_playlist_id)
        adjacency.setdefault(source, set()).add(target)
    candidate_source = (source_provider, source_account_id, source_playlist_id)
    candidate_target = (target_provider, target_account_id, target_playlist_id)
    pending = [candidate_target]
    visited: set[tuple[str, str, str]] = set()
    while pending:
        endpoint = pending.pop()
        if endpoint == candidate_source:
            raise HTTPException(
                status_code=409,
                detail="sync rule would create a feedback loop",
            )
        if endpoint in visited:
            continue
        visited.add(endpoint)
        pending.extend(adjacency.get(endpoint, ()))


async def _lock_rule_graph(session: AsyncSession, user_id: str) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    digest = hashlib.blake2b(
        f"open-playlist-engine:sync-graph:{user_id}".encode(),
        digest_size=8,
    ).digest()
    key = int.from_bytes(digest, byteorder="big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def _ensure_no_unresolved_review(
    session: AsyncSession,
    rule_id: str,
) -> None:
    checkpoint = await session.get(orm.SyncCheckpoint, rule_id)
    if checkpoint and checkpoint.unresolved:
        raise HTTPException(
            status_code=409,
            detail="resolve the current sync review before starting or resuming the rule",
        )


async def _ensure_not_active(session: AsyncSession, rule_id: str) -> None:
    active = await session.scalar(
        select(orm.SyncRun.id).where(
            orm.SyncRun.rule_id == rule_id,
            orm.SyncRun.status.in_(["queued", "running"]),
        )
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="sync rule is currently running")


async def _owned_rule(
    session: AsyncSession,
    rule_id: str,
    user_id: str,
    *,
    for_update: bool = False,
) -> orm.SyncRule | None:
    stmt = select(orm.SyncRule).where(
        orm.SyncRule.id == rule_id,
        orm.SyncRule.user_id == user_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def _enqueue_or_inline(
    background_tasks: BackgroundTasks,
    run: orm.SyncRun,
) -> None:
    try:
        redis = await create_pool(RedisSettings.from_dsn(get_settings().valkey_url))
        try:
            await redis.enqueue_job("run_sync", run.id, _job_id=run.queue_job_id)
        finally:
            await redis.close(close_connection_pool=True)
    except (ConnectionError, OSError, RedisError, TimeoutError) as exc:
        logger.warning("queue unavailable; running sync inline run_id=%s error=%s", run.id, exc)
        background_tasks.add_task(run_sync, {}, run.id)
