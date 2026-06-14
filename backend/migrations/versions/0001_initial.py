"""Initial Open Playlist Engine schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_account",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_user_id", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", "provider_user_id"),
    )
    op.create_index(op.f("ix_provider_account_provider"), "provider_account", ["provider"])
    op.create_index(op.f("ix_provider_account_user_id"), "provider_account", ["user_id"])

    op.create_table(
        "migration_job",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("source_provider", sa.String(), nullable=False),
        sa.Column("target_provider", sa.String(), nullable=False),
        sa.Column("source_account_id", sa.String(), nullable=False),
        sa.Column("target_account_id", sa.String(), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_migration_job_status"), "migration_job", ["status"])
    op.create_index(op.f("ix_migration_job_user_id"), "migration_job", ["user_id"])

    op.create_table(
        "track_identity",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("isrc", sa.String(), nullable=True),
        sa.Column("upc", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False),
        sa.Column("album", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_track_identity_isrc"), "track_identity", ["isrc"])

    op.create_table(
        "provider_credential",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("auth_kind", sa.String(), nullable=False),
        sa.Column("enc_blob", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["provider_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "job_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("source_playlist_id", sa.String(), nullable=False),
        sa.Column("source_playlist_name", sa.String(), nullable=True),
        sa.Column("target_playlist_id", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False),
        sa.Column("isrc", sa.String(), nullable=True),
        sa.Column("target_uri", sa.String(), nullable=True),
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
        sa.ForeignKeyConstraint(["job_id"], ["migration_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_item_job_id"), "job_item", ["job_id"])
    op.create_index(op.f("ix_job_item_status"), "job_item", ["status"])

    op.create_table(
        "operation_ledger",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("op", sa.String(), nullable=False),
        sa.Column("intent", sa.JSON(), nullable=False),
        sa.Column("observed_target_id", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["migration_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_operation_ledger_job_id"), "operation_ledger", ["job_id"])

    op.create_table(
        "track_edge",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("identity_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_track_id", sa.String(), nullable=False),
        sa.Column("provider_uri", sa.String(), nullable=False),
        sa.Column("market", sa.String(), nullable=True),
        sa.Column("explicit", sa.Boolean(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("verified_by_user", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["identity_id"], ["track_identity.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_track_id"),
    )
    op.create_index(op.f("ix_track_edge_provider"), "track_edge", ["provider"])


def downgrade() -> None:
    op.drop_index(op.f("ix_track_edge_provider"), table_name="track_edge")
    op.drop_table("track_edge")
    op.drop_index(op.f("ix_operation_ledger_job_id"), table_name="operation_ledger")
    op.drop_table("operation_ledger")
    op.drop_index(op.f("ix_job_item_status"), table_name="job_item")
    op.drop_index(op.f("ix_job_item_job_id"), table_name="job_item")
    op.drop_table("job_item")
    op.drop_table("provider_credential")
    op.drop_index(op.f("ix_track_identity_isrc"), table_name="track_identity")
    op.drop_table("track_identity")
    op.drop_index(op.f("ix_migration_job_user_id"), table_name="migration_job")
    op.drop_index(op.f("ix_migration_job_status"), table_name="migration_job")
    op.drop_table("migration_job")
    op.drop_index(op.f("ix_provider_account_user_id"), table_name="provider_account")
    op.drop_index(op.f("ix_provider_account_provider"), table_name="provider_account")
    op.drop_table("provider_account")
