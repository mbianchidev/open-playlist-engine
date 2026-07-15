"""arq worker entry point.

Run with: ``arq app.jobs.worker.WorkerSettings``
"""

from __future__ import annotations

import logging

from arq.connections import RedisSettings

import app.providers  # noqa: F401  (registers adapters in the worker process)
from app.jobs.migration import run_migration
from app.jobs.organizer import run_organizer
from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


class WorkerSettings:
    functions = [run_migration, run_organizer]
    redis_settings = RedisSettings.from_dsn(get_settings().valkey_url)
    job_timeout = max(
        get_settings().migration_worker_job_timeout_s,
        get_settings().organizer_worker_job_timeout_s,
    )
