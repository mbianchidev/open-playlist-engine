"""Cache provider playlist refs and track snapshots.

Revision ID: 0002_playlist_read_cache
Revises: 0001_initial
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_playlist_read_cache"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cached_playlist_ref",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("track_count", sa.Integer(), nullable=True),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("collaborative", sa.Boolean(), nullable=True),
        sa.Column("snapshot_id", sa.String(), nullable=True),
        sa.Column("tracks_href", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["provider_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", "account_id", "playlist_id"),
    )
    op.create_index(op.f("ix_cached_playlist_ref_account_id"), "cached_playlist_ref", ["account_id"])
    op.create_index(op.f("ix_cached_playlist_ref_playlist_id"), "cached_playlist_ref", ["playlist_id"])
    op.create_index(op.f("ix_cached_playlist_ref_provider"), "cached_playlist_ref", ["provider"])
    op.create_index(op.f("ix_cached_playlist_ref_user_id"), "cached_playlist_ref", ["user_id"])

    op.create_table(
        "cached_playlist_tracks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("tracks", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["provider_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", "account_id", "playlist_id"),
    )
    op.create_index(
        op.f("ix_cached_playlist_tracks_account_id"), "cached_playlist_tracks", ["account_id"]
    )
    op.create_index(
        op.f("ix_cached_playlist_tracks_playlist_id"), "cached_playlist_tracks", ["playlist_id"]
    )
    op.create_index(op.f("ix_cached_playlist_tracks_provider"), "cached_playlist_tracks", ["provider"])
    op.create_index(op.f("ix_cached_playlist_tracks_user_id"), "cached_playlist_tracks", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_cached_playlist_tracks_user_id"), table_name="cached_playlist_tracks")
    op.drop_index(op.f("ix_cached_playlist_tracks_provider"), table_name="cached_playlist_tracks")
    op.drop_index(op.f("ix_cached_playlist_tracks_playlist_id"), table_name="cached_playlist_tracks")
    op.drop_index(op.f("ix_cached_playlist_tracks_account_id"), table_name="cached_playlist_tracks")
    op.drop_table("cached_playlist_tracks")
    op.drop_index(op.f("ix_cached_playlist_ref_user_id"), table_name="cached_playlist_ref")
    op.drop_index(op.f("ix_cached_playlist_ref_provider"), table_name="cached_playlist_ref")
    op.drop_index(op.f("ix_cached_playlist_ref_playlist_id"), table_name="cached_playlist_ref")
    op.drop_index(op.f("ix_cached_playlist_ref_account_id"), table_name="cached_playlist_ref")
    op.drop_table("cached_playlist_ref")
