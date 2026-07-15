"""Queue durable migration jobs, with an inline fallback for local development."""

from __future__ import annotations

import logging

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import BackgroundTasks
from redis.exceptions import RedisError

from app.jobs.migration import run_migration
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def enqueue_migration(background_tasks: BackgroundTasks, job_id: str) -> None:
    try:
        redis = await create_pool(RedisSettings.from_dsn(get_settings().valkey_url))
        try:
            await redis.enqueue_job("run_migration", job_id)
        finally:
            await redis.close(close_connection_pool=True)
    except (ConnectionError, OSError, RedisError, TimeoutError) as exc:
        logger.warning(
            "queue unavailable; running migration inline job_id=%s error_type=%s",
            job_id,
            type(exc).__name__,
        )
        background_tasks.add_task(run_migration, {}, job_id)
