"""Deterministic snapshot retention selection."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.db import models as orm


def retention_candidates(
    snapshots: list[orm.LibrarySnapshot],
    *,
    now: datetime,
    retention_count: int | None,
    retention_days: int | None,
) -> list[orm.LibrarySnapshot]:
    eligible = [
        snapshot
        for snapshot in snapshots
        if snapshot.status in {"complete", "partial"} and snapshot.archive_name
    ]
    eligible.sort(
        key=lambda snapshot: (
            snapshot.created_at or datetime.min.replace(tzinfo=now.tzinfo),
            snapshot.id,
        ),
        reverse=True,
    )
    if len(eligible) <= 1:
        return []

    keep_newest = eligible[0]
    count_overflow = set()
    if retention_count is not None:
        count_overflow = {snapshot.id for snapshot in eligible[max(1, retention_count) :]}

    age_overflow = set()
    if retention_days is not None:
        cutoff = now - timedelta(days=retention_days)
        age_overflow = {
            snapshot.id
            for snapshot in eligible
            if snapshot.id != keep_newest.id
            and snapshot.created_at is not None
            and _comparable(snapshot.created_at, now) < cutoff
        }

    delete_ids = count_overflow | age_overflow
    return [snapshot for snapshot in reversed(eligible) if snapshot.id in delete_ids]


def _comparable(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value
