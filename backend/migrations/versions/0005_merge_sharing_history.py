"""Merge playlist sharing and migration history branches.

Revision ID: 0005_merge_sharing_history
Revises: 0003_playlist_sharing, 0004_migration_history_reports
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0005_merge_sharing_history"
down_revision: tuple[str, str] = (
    "0003_playlist_sharing",
    "0004_migration_history_reports",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
