"""Merge local snapshot and public source import heads.

Revision ID: 0010_merge_snapshots
Revises: 0009_public_source_imports, 0003_local_library_snapshots
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0010_merge_snapshots"
down_revision: tuple[str, str] = (
    "0009_public_source_imports",
    "0003_local_library_snapshots",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
