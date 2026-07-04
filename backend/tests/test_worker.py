from __future__ import annotations

from app.db import models as orm
from app.jobs import migration
from app.jobs.worker import WorkerSettings
from app.settings import get_settings


class _JobSession:
    def __init__(self, job: orm.MigrationJob | None) -> None:
        self.job = job
        self.committed = False

    async def get(self, model: type[orm.MigrationJob], job_id: str) -> orm.MigrationJob | None:
        if model is not orm.MigrationJob:
            return None
        if self.job and self.job.id == job_id:
            return self.job
        return None

    async def commit(self) -> None:
        self.committed = True


def test_worker_timeout_uses_migration_setting() -> None:
    assert WorkerSettings.job_timeout == get_settings().migration_worker_job_timeout_s
    assert WorkerSettings.job_timeout >= 3600


async def test_mark_job_failed_persists_timeout_error() -> None:
    job = orm.MigrationJob(id="job", user_id="local", status="running", selection={})
    session = _JobSession(job)

    await migration._mark_job_failed(session, "job", "migration cancelled or timed out")

    assert job.status == "failed"
    assert job.error == "migration cancelled or timed out"
    assert session.committed is True
