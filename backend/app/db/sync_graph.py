"""Synchronization graph locking and pending-edge discovery."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as orm


@dataclass(frozen=True)
class PendingContinuousSync:
    source: tuple[str, str, str]
    target_account: tuple[str, str]


async def lock_sync_graph(session: AsyncSession, user_id: str) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    digest = hashlib.blake2b(
        f"open-playlist-engine:sync-graph:{user_id}".encode(),
        digest_size=8,
    ).digest()
    key = int.from_bytes(digest, byteorder="big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def pending_continuous_syncs(
    session: AsyncSession,
    *,
    user_id: str,
) -> list[PendingContinuousSync]:
    jobs = list(
        (
            await session.execute(
                select(orm.MigrationJob).where(
                    orm.MigrationJob.user_id == user_id,
                    orm.MigrationJob.origin == "manual",
                    orm.MigrationJob.status.in_(["pending", "running", "done"]),
                )
            )
        ).scalars()
    )
    edges = []
    for job in jobs:
        selection = job.selection if isinstance(job.selection, dict) else {}
        intent = selection.get("continuous_sync")
        playlist_ids = selection.get("playlist_ids")
        if not isinstance(intent, dict) or not isinstance(playlist_ids, list):
            continue
        if len(playlist_ids) != 1:
            continue
        if job.status == "done" and intent.get("status") in {"active", "failed"}:
            continue
        edges.append(
            PendingContinuousSync(
                source=(
                    job.source_provider,
                    job.source_account_id,
                    str(playlist_ids[0]),
                ),
                target_account=(job.target_provider, job.target_account_id),
            )
        )
    return edges
