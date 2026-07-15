"""arq worker entry point.

Run with: ``arq app.jobs.worker.WorkerSettings``
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings

import app.providers  # noqa: F401  (registers adapters in the worker process)
from app.imports.service import cleanup_local_imports
from app.jobs.migration import run_migration
from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


class WorkerSettings:
    functions = [run_migration]
    cron_jobs = [cron(cleanup_local_imports, run_at_startup=True)]
    redis_settings = RedisSettings.from_dsn(get_settings().valkey_url)
    job_timeout = get_settings().migration_worker_job_timeout_s
