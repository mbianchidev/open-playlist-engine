"""Add playlist organizer jobs and cached ownership metadata.

Revision ID: 0003_playlist_organizer
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_playlist_organizer"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cached_playlist_ref", sa.Column("owner_name", sa.String(), nullable=True))
    op.add_column("cached_playlist_ref", sa.Column("is_owned", sa.Boolean(), nullable=True))
    op.add_column("cached_playlist_ref", sa.Column("is_followed", sa.Boolean(), nullable=True))
    op.add_column(
        "cached_playlist_ref",
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "cached_playlist_ref",
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "organizer_job",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
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
    op.create_index(op.f("ix_organizer_job_account_id"), "organizer_job", ["account_id"])
    op.create_index(op.f("ix_organizer_job_provider"), "organizer_job", ["provider"])
    op.create_index(op.f("ix_organizer_job_status"), "organizer_job", ["status"])
    op.create_index(op.f("ix_organizer_job_user_id"), "organizer_job", ["user_id"])

    op.create_table(
        "organizer_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("playlist_name", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("destructive", sa.Boolean(), nullable=False),
        sa.Column("ownership", sa.String(), nullable=False),
        sa.Column("collaborative", sa.Boolean(), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
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
        sa.ForeignKeyConstraint(["job_id"], ["organizer_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "playlist_id", "action"),
    )
    op.create_index(op.f("ix_organizer_item_job_id"), "organizer_item", ["job_id"])
    op.create_index(op.f("ix_organizer_item_playlist_id"), "organizer_item", ["playlist_id"])
    op.create_index(op.f("ix_organizer_item_status"), "organizer_item", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_organizer_item_status"), table_name="organizer_item")
    op.drop_index(op.f("ix_organizer_item_playlist_id"), table_name="organizer_item")
    op.drop_index(op.f("ix_organizer_item_job_id"), table_name="organizer_item")
    op.drop_table("organizer_item")
    op.drop_index(op.f("ix_organizer_job_user_id"), table_name="organizer_job")
    op.drop_index(op.f("ix_organizer_job_status"), table_name="organizer_job")
    op.drop_index(op.f("ix_organizer_job_provider"), table_name="organizer_job")
    op.drop_index(op.f("ix_organizer_job_account_id"), table_name="organizer_job")
    op.drop_table("organizer_job")

    op.drop_column("cached_playlist_ref", "provider_updated_at")
    op.drop_column("cached_playlist_ref", "provider_created_at")
    op.drop_column("cached_playlist_ref", "is_followed")
    op.drop_column("cached_playlist_ref", "is_owned")
    op.drop_column("cached_playlist_ref", "owner_name")
