"""arq worker entry point.

Run with: ``arq app.jobs.worker.WorkerSettings``
"""

from __future__ import annotations

import logging

from arq import cron, func
from arq.connections import RedisSettings

import app.providers  # noqa: F401  (registers adapters in the worker process)
from app.imports.service import cleanup_local_imports
from app.jobs.history_cleanup import cleanup_expired_migration_details
from app.jobs.migration import run_migration
from app.jobs.organizer import run_organizer
from app.jobs.sync import finalize_sync_review, run_sync, schedule_syncs
from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


class WorkerSettings:
    functions = [
        func(run_migration, timeout=get_settings().migration_worker_job_timeout_s),
        func(run_organizer, timeout=get_settings().organizer_worker_job_timeout_s),
        func(run_sync, timeout=get_settings().migration_worker_job_timeout_s),
        func(finalize_sync_review, timeout=get_settings().migration_worker_job_timeout_s),
    ]
    cron_jobs = [
        cron(schedule_syncs, minute=None, second=0, run_at_startup=True),
        cron(cleanup_expired_migration_details, minute=17),
        cron(cleanup_local_imports, run_at_startup=True),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().valkey_url)
    job_timeout = max(
        get_settings().migration_worker_job_timeout_s,
        get_settings().organizer_worker_job_timeout_s,
    )
