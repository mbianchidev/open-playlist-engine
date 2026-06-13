"""arq worker entry point.

Run with: ``arq app.jobs.worker.WorkerSettings``
"""

from __future__ import annotations

from arq.connections import RedisSettings

from app.jobs.migration import run_migration
from app.settings import get_settings


class WorkerSettings:
    functions = [run_migration]
    redis_settings = RedisSettings.from_dsn(get_settings().valkey_url)
