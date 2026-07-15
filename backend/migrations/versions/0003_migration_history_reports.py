"""Add detailed migration history and retained review decisions.

Revision ID: 0003_migration_history_reports
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_migration_history_reports"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "migration_job",
        sa.Column("warnings", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column(
        "migration_job",
        sa.Column("result_summary", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "migration_job", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "migration_job", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "migration_job",
        sa.Column("details_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "migration_job",
        sa.Column("details_purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_migration_job_details_expires_at"),
        "migration_job",
        ["details_expires_at"],
    )

    op.add_column("job_item", sa.Column("review_action", sa.String(), nullable=True))
    op.add_column("job_item", sa.Column("review_original_status", sa.String(), nullable=True))
    op.add_column("job_item", sa.Column("review_original_reason", sa.String(), nullable=True))
    op.add_column(
        "job_item", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "review_decision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("source_provider", sa.String(), nullable=False),
        sa.Column("target_provider", sa.String(), nullable=False),
        sa.Column("source_account_id", sa.String(), nullable=False),
        sa.Column("target_account_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False),
        sa.Column("album", sa.String(), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=True),
        sa.Column("isrc", sa.String(), nullable=True),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("target_uri", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["migration_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_review_decision_job_id"), "review_decision", ["job_id"])
    op.create_index(op.f("ix_review_decision_user_id"), "review_decision", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_review_decision_user_id"), table_name="review_decision")
    op.drop_index(op.f("ix_review_decision_job_id"), table_name="review_decision")
    op.drop_table("review_decision")

    op.drop_column("job_item", "reviewed_at")
    op.drop_column("job_item", "review_original_reason")
    op.drop_column("job_item", "review_original_status")
    op.drop_column("job_item", "review_action")

    op.drop_index(op.f("ix_migration_job_details_expires_at"), table_name="migration_job")
    op.drop_column("migration_job", "details_purged_at")
    op.drop_column("migration_job", "details_expires_at")
    op.drop_column("migration_job", "completed_at")
    op.drop_column("migration_job", "started_at")
    op.drop_column("migration_job", "result_summary")
    op.drop_column("migration_job", "warnings")
