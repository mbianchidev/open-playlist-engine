"""Create a durable sync rule after a requested migration succeeds."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as orm

logger = logging.getLogger(__name__)


async def create_sync_rule(*args, **kwargs):
    from app.api.syncs import create_sync_rule as create

    return await create(*args, **kwargs)


async def ensure_continuous_sync(
    session: AsyncSession,
    job: orm.MigrationJob,
) -> str | None:
    selection = dict(job.selection or {})
    raw_intent = selection.get("continuous_sync")
    if job.origin != "manual" or job.status != "done" or not isinstance(raw_intent, dict):
        return None
    intent = dict(raw_intent)
    existing_rule_id = intent.get("sync_rule_id")
    if intent.get("status") == "active" and isinstance(existing_rule_id, str):
        return existing_rule_id
    unresolved_item = await session.scalar(
        select(orm.JobItem.id)
        .where(
            orm.JobItem.job_id == job.id,
            orm.JobItem.status.in_(["needs_review", "failed"]),
        )
        .limit(1)
    )
    if unresolved_item is not None:
        return None

    from app.api.syncs import CreateSyncRule

    body = CreateSyncRule(
        migration_job_id=job.id,
        mode=str(intent.get("mode") or "add_only"),
        cadence_minutes=int(intent.get("cadence_minutes") or 60),
        timezone=str(intent.get("timezone") or "UTC"),
    )
    try:
        rule = await create_sync_rule(
            body,
            session=session,
            user_id=job.user_id,
            allow_existing=True,
        )
    except HTTPException as exc:
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        intent.update(status="failed", sync_rule_id=None, error=message)
        selection["continuous_sync"] = intent
        job.selection = selection
        await session.commit()
        logger.warning(
            "continuous sync setup failed migration_job_id=%s error=%s",
            job.id,
            message,
        )
        return None

    intent.update(status="active", sync_rule_id=rule.id, error=None)
    selection["continuous_sync"] = intent
    job.selection = selection
    await session.commit()
    logger.info(
        "continuous sync active migration_job_id=%s sync_rule_id=%s",
        job.id,
        rule.id,
    )
    return rule.id
