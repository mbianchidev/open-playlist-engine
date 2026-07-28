"""Scheduled retention cleanup for item-level migration history."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select

from app.db import models as orm
from app.db.base import get_sessionmaker
from app.db.migration_history import (
    TERMINAL_JOB_STATUSES,
    collect_job_result_summary,
    utcnow,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def cleanup_expired_migration_details(ctx: dict) -> int:
    settings = get_settings()
    if settings.migration_history_retention_days <= 0:
        return 0

    now = utcnow()
    batch_size = settings.migration_history_cleanup_batch_size
    async with get_sessionmaker()() as session:
        missing_deadlines = list(
            (
                await session.execute(
                    select(orm.MigrationJob)
                    .where(
                        orm.MigrationJob.status.in_(TERMINAL_JOB_STATUSES),
                        orm.MigrationJob.details_expires_at.is_(None),
                        orm.MigrationJob.details_purged_at.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                    .order_by(orm.MigrationJob.created_at, orm.MigrationJob.id)
                    .limit(batch_size)
                )
            ).scalars()
        )
        for job in missing_deadlines:
            completed_at = job.completed_at or job.created_at
            if completed_at is not None:
                job.details_expires_at = completed_at + timedelta(
                    days=settings.migration_history_retention_days
                )
        if missing_deadlines:
            await session.commit()

        expired_jobs = list(
            (
                await session.execute(
                    select(orm.MigrationJob)
                    .where(
                        orm.MigrationJob.details_expires_at <= now,
                        orm.MigrationJob.details_purged_at.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                    .order_by(orm.MigrationJob.details_expires_at, orm.MigrationJob.id)
                    .limit(batch_size)
                )
            ).scalars()
        )
        for job in expired_jobs:
            job.result_summary = await collect_job_result_summary(session, job)
            await session.execute(
                delete(orm.OperationLedger).where(orm.OperationLedger.job_id == job.id)
            )
            await session.execute(delete(orm.JobItem).where(orm.JobItem.job_id == job.id))
            job.details_purged_at = now
        if expired_jobs:
            await session.commit()

    if expired_jobs:
        logger.info("purged item-level migration history jobs=%s", len(expired_jobs))
    return len(expired_jobs)
