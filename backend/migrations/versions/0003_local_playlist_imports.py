"""Store expiring normalized local playlist-file imports.

Revision ID: 0003_local_playlist_imports
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_local_playlist_imports"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_playlist_import",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("detected_format", sa.String(), nullable=False),
        sa.Column("encoding", sa.String(), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("queued_job_id", sa.String(), nullable=True),
        sa.Column("playlists", sa.JSON(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("playlist_count", sa.Integer(), nullable=False),
        sa.Column("track_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("malformed_count", sa.Integer(), nullable=False),
        sa.Column("unsupported_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_local_playlist_import_expires_at"),
        "local_playlist_import",
        ["expires_at"],
    )
    op.create_index(
        op.f("ix_local_playlist_import_queued_job_id"),
        "local_playlist_import",
        ["queued_job_id"],
    )
    op.create_index(
        op.f("ix_local_playlist_import_status"),
        "local_playlist_import",
        ["status"],
    )
    op.create_index(
        op.f("ix_local_playlist_import_user_id"),
        "local_playlist_import",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_local_playlist_import_user_id"), table_name="local_playlist_import"
    )
    op.drop_index(
        op.f("ix_local_playlist_import_status"), table_name="local_playlist_import"
    )
    op.drop_index(
        op.f("ix_local_playlist_import_queued_job_id"),
        table_name="local_playlist_import",
    )
    op.drop_index(
        op.f("ix_local_playlist_import_expires_at"),
        table_name="local_playlist_import",
    )
    op.drop_table("local_playlist_import")
