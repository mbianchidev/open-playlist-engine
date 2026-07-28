from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db import models as orm
from app.snapshots.retention import retention_candidates


def _snapshot(snapshot_id: str, *, created_at: datetime, status: str = "complete"):
    return orm.LibrarySnapshot(
        id=snapshot_id,
        user_id="local",
        profile_id="profile",
        bundle_id=snapshot_id,
        library_id="library",
        source_providers=["spotify"],
        source_labels=["Personal Spotify"],
        status=status,
        schema_version=1,
        archive_name=f"{snapshot_id}.opb",
        size_bytes=1,
        manifest={},
        errors=[],
        created_at=created_at,
    )


def test_retention_is_deterministic_for_equal_timestamps() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    rows = [
        _snapshot("a", created_at=now),
        _snapshot("c", created_at=now),
        _snapshot("b", created_at=now),
    ]

    candidates = retention_candidates(
        rows,
        now=now,
        retention_count=2,
        retention_days=None,
    )

    assert [row.id for row in candidates] == ["a"]


def test_retention_applies_count_or_age_and_keeps_newest_snapshot() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    rows = [
        _snapshot("newest", created_at=now - timedelta(days=10)),
        _snapshot("second", created_at=now - timedelta(days=11)),
        _snapshot("third", created_at=now - timedelta(days=12)),
    ]

    candidates = retention_candidates(
        rows,
        now=now,
        retention_count=2,
        retention_days=1,
    )

    assert [row.id for row in candidates] == ["third", "second"]


def test_retention_ignores_failed_or_active_rows() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    rows = [
        _snapshot("complete", created_at=now - timedelta(days=2)),
        _snapshot("partial", created_at=now - timedelta(days=3), status="partial"),
        _snapshot("failed", created_at=now - timedelta(days=4), status="failed"),
        _snapshot("running", created_at=now - timedelta(days=5), status="running"),
    ]

    candidates = retention_candidates(
        rows,
        now=now,
        retention_count=1,
        retention_days=None,
    )

    assert [row.id for row in candidates] == ["partial"]
