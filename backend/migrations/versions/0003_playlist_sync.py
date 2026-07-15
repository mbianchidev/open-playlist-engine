"""Add persistent scheduled playlist synchronization.

Revision ID: 0003_playlist_sync
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_playlist_sync"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_rule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("source_provider", sa.String(), nullable=False),
        sa.Column("source_account_id", sa.String(), nullable=False),
        sa.Column("source_playlist_id", sa.String(), nullable=False),
        sa.Column("source_playlist_name", sa.String(), nullable=False),
        sa.Column("target_provider", sa.String(), nullable=False),
        sa.Column("target_account_id", sa.String(), nullable=False),
        sa.Column("target_playlist_id", sa.String(), nullable=False),
        sa.Column("target_playlist_name", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("cadence_minutes", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_added", sa.Integer(), nullable=False),
        sa.Column("last_removed", sa.Integer(), nullable=False),
        sa.Column("last_reordered", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "source_provider",
            "source_account_id",
            "source_playlist_id",
            "target_provider",
            "target_account_id",
            "target_playlist_id",
            name="uq_sync_rule_endpoint_pair",
        ),
    )
    op.create_index(op.f("ix_sync_rule_enabled"), "sync_rule", ["enabled"])
    op.create_index(op.f("ix_sync_rule_next_run_at"), "sync_rule", ["next_run_at"])
    op.create_index(op.f("ix_sync_rule_status"), "sync_rule", ["status"])
    op.create_index(op.f("ix_sync_rule_user_id"), "sync_rule", ["user_id"])

    op.create_table(
        "sync_run",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("lease_token", sa.String(), nullable=False),
        sa.Column("queue_job_id", sa.String(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("target_before", sa.JSON(), nullable=False),
        sa.Column("target_after", sa.JSON(), nullable=False),
        sa.Column("added", sa.Integer(), nullable=False),
        sa.Column("removed", sa.Integer(), nullable=False),
        sa.Column("reordered", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["sync_rule.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("queue_job_id"),
    )
    op.create_index(op.f("ix_sync_run_rule_id"), "sync_run", ["rule_id"])
    op.create_index(op.f("ix_sync_run_status"), "sync_run", ["status"])
    op.create_index(
        "uq_sync_run_active_rule",
        "sync_run",
        ["rule_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "sync_checkpoint",
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("target_snapshot", sa.JSON(), nullable=False),
        sa.Column("mappings", sa.JSON(), nullable=False),
        sa.Column("unresolved", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["sync_rule.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rule_id"),
    )

    op.add_column(
        "migration_job",
        sa.Column("origin", sa.String(), server_default="manual", nullable=False),
    )
    op.add_column("migration_job", sa.Column("sync_run_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_migration_job_sync_run_id",
        "migration_job",
        "sync_run",
        ["sync_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_migration_job_origin"), "migration_job", ["origin"])
    op.create_index(
        op.f("ix_migration_job_sync_run_id"),
        "migration_job",
        ["sync_run_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_migration_job_sync_run_id"), table_name="migration_job")
    op.drop_index(op.f("ix_migration_job_origin"), table_name="migration_job")
    op.drop_constraint("fk_migration_job_sync_run_id", "migration_job", type_="foreignkey")
    op.drop_column("migration_job", "sync_run_id")
    op.drop_column("migration_job", "origin")
    op.drop_table("sync_checkpoint")
    op.drop_index("uq_sync_run_active_rule", table_name="sync_run")
    op.drop_index(op.f("ix_sync_run_status"), table_name="sync_run")
    op.drop_index(op.f("ix_sync_run_rule_id"), table_name="sync_run")
    op.drop_table("sync_run")
    op.drop_index(op.f("ix_sync_rule_user_id"), table_name="sync_rule")
    op.drop_index(op.f("ix_sync_rule_status"), table_name="sync_rule")
    op.drop_index(op.f("ix_sync_rule_next_run_at"), table_name="sync_rule")
    op.drop_index(op.f("ix_sync_rule_enabled"), table_name="sync_rule")
    op.drop_table("sync_rule")
