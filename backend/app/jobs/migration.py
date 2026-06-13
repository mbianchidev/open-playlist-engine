"""The migration pipeline: import -> match -> review -> write.

Each phase is resumable. Writes go through the operation ledger
(:class:`app.db.models.OperationLedger`): we persist intent, call the provider,
persist the observed target id/position, and on uncertain failure reconcile by
*reading* target state instead of blindly retrying a non-idempotent call.
Progress is derived from ``job_item`` rows so a disconnected client can replay.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class Phase(StrEnum):
    IMPORT = "import"
    MATCH = "match"
    REVIEW = "review"
    WRITE = "write"
    DONE = "done"


async def run_migration(ctx: dict, job_id: str) -> None:
    """Entry point invoked by the arq worker.

    Skeleton — see TODOs. Kept importable so the worker wiring and tests load.
    """
    logger.info("starting migration job_id=%s", job_id)

    # PHASE 1 — IMPORT
    # for each selected source playlist: read via source adapter -> Open Playlist,
    # persist job_item rows (capture ISRC, position, media_type). Flag
    # non-migratable items with unsupported_reason for the lossy report.
    # TODO

    # PHASE 2 — MATCH
    # for each migratable item: MatchService.resolve(track, target, cred).
    # auto-approve confidence >= threshold; else mark needs_review.
    # TODO

    # PHASE 3 — REVIEW
    # if any needs_review and auto-review disabled: pause job, wait for user.
    # TODO

    # PHASE 4 — WRITE
    # create_playlist (ledger: intent -> observed id), then add_tracks in batches
    # bounded by capabilities.max_add_batch, recording per-item results and
    # respecting the central rate limiter.
    # TODO

    logger.info("migration job_id=%s reached %s", job_id, Phase.WRITE)
