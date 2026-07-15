"""Persist public URL and text import snapshots.

Revision ID: 0003_imported_playlists
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_imported_playlists"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "imported_playlist",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("source_provider", sa.String(), nullable=False),
        sa.Column("source_label", sa.String(), nullable=False),
        sa.Column("source_locator", sa.String(), nullable=False),
        sa.Column("source_fingerprint", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("playlist", sa.JSON(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_imported_playlist_playlist_id"),
        "imported_playlist",
        ["playlist_id"],
    )
    op.create_index(
        op.f("ix_imported_playlist_source_fingerprint"),
        "imported_playlist",
        ["source_fingerprint"],
    )
    op.create_index(
        op.f("ix_imported_playlist_source_provider"),
        "imported_playlist",
        ["source_provider"],
    )
    op.create_index(
        op.f("ix_imported_playlist_user_id"),
        "imported_playlist",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_imported_playlist_user_id"),
        table_name="imported_playlist",
    )
    op.drop_index(
        op.f("ix_imported_playlist_source_provider"),
        table_name="imported_playlist",
    )
    op.drop_index(
        op.f("ix_imported_playlist_source_fingerprint"),
        table_name="imported_playlist",
    )
    op.drop_index(
        op.f("ix_imported_playlist_playlist_id"),
        table_name="imported_playlist",
    )
    op.drop_table("imported_playlist")
