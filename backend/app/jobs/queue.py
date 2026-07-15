from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import BackgroundTasks
from redis.exceptions import RedisError

from app.settings import get_settings

logger = logging.getLogger(__name__)


async def enqueue_or_inline(
    background_tasks: BackgroundTasks,
    *,
    function_name: str,
    fallback: Callable[[dict, str], Awaitable[None]],
    job_id: str,
    job_label: str,
) -> None:
    try:
        redis = await create_pool(RedisSettings.from_dsn(get_settings().valkey_url))
        try:
            await redis.enqueue_job(function_name, job_id)
        finally:
            await redis.close(close_connection_pool=True)
    except (ConnectionError, OSError, RedisError, TimeoutError) as exc:
        logger.warning(
            "queue unavailable; running %s inline job_id=%s error=%s",
            job_label,
            job_id,
            exc,
        )
        background_tasks.add_task(fallback, {}, job_id)
