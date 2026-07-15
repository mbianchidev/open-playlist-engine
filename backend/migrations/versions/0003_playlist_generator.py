"""Add private playlist generator preferences and review drafts.

Revision ID: 0003_playlist_generator
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_playlist_generator"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_preference",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "generation_draft",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("target_provider", sa.String(), nullable=False),
        sa.Column("target_account_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("model_backend", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("confirmed_job_id", sa.String(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["target_account_id"],
            ["provider_account.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_generation_draft_target_account_id"),
        "generation_draft",
        ["target_account_id"],
    )
    op.create_index(op.f("ix_generation_draft_status"), "generation_draft", ["status"])
    op.create_index(op.f("ix_generation_draft_user_id"), "generation_draft", ["user_id"])

    op.create_table(
        "generation_draft_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("draft_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("intent_title", sa.String(), nullable=False),
        sa.Column("intent_artist", sa.String(), nullable=False),
        sa.Column("intent_album", sa.String(), nullable=True),
        sa.Column("intent_reason", sa.String(), nullable=True),
        sa.Column("provider_track_id", sa.String(), nullable=True),
        sa.Column("target_uri", sa.String(), nullable=True),
        sa.Column("resolved_title", sa.String(), nullable=True),
        sa.Column("resolved_artist", sa.String(), nullable=True),
        sa.Column("resolved_album", sa.String(), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=True),
        sa.Column("isrc", sa.String(), nullable=True),
        sa.Column("explicit", sa.Boolean(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["generation_draft.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "position"),
    )
    op.create_index(
        op.f("ix_generation_draft_item_draft_id"),
        "generation_draft_item",
        ["draft_id"],
    )
    op.create_index(
        op.f("ix_generation_draft_item_status"),
        "generation_draft_item",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_generation_draft_item_status"),
        table_name="generation_draft_item",
    )
    op.drop_index(
        op.f("ix_generation_draft_item_draft_id"),
        table_name="generation_draft_item",
    )
    op.drop_table("generation_draft_item")
    op.drop_index(op.f("ix_generation_draft_user_id"), table_name="generation_draft")
    op.drop_index(op.f("ix_generation_draft_status"), table_name="generation_draft")
    op.drop_index(
        op.f("ix_generation_draft_target_account_id"),
        table_name="generation_draft",
    )
    op.drop_table("generation_draft")
    op.drop_table("generation_preference")
