"""arq worker entry point.

Run with: ``arq app.jobs.worker.WorkerSettings``
"""

from __future__ import annotations

import logging

from arq.connections import RedisSettings
from arq.cron import cron

import app.providers  # noqa: F401  (registers adapters in the worker process)
from app.jobs.migration import run_migration
from app.jobs.sync import finalize_sync_review, run_sync, schedule_syncs
from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


class WorkerSettings:
    functions = [run_migration, run_sync, finalize_sync_review]
    cron_jobs = [cron(schedule_syncs, minute=None, second=0, run_at_startup=True)]
    redis_settings = RedisSettings.from_dsn(get_settings().valkey_url)
    job_timeout = get_settings().migration_worker_job_timeout_s
