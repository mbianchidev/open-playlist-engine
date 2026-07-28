"""Merge scheduled sync with organizer migration history.

Revision ID: 0007_merge_sync
Revises: 0006_playlist_organizer, 0003_playlist_sync
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0007_merge_sync"
down_revision: tuple[str, str] = ("0006_playlist_organizer", "0003_playlist_sync")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
