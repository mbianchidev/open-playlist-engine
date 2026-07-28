"""Add local library snapshot profiles, archives, and restore sources.

Revision ID: 0003_local_library_snapshots
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_local_library_snapshots"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "snapshot_profile",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("retention_count", sa.Integer(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
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
    op.create_index(op.f("ix_snapshot_profile_user_id"), "snapshot_profile", ["user_id"])

    op.create_table(
        "snapshot_profile_source",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("account_label", sa.String(), nullable=True),
        sa.Column("collection_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["provider_account.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["snapshot_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "provider", "account_id"),
    )
    op.create_index(
        op.f("ix_snapshot_profile_source_account_id"),
        "snapshot_profile_source",
        ["account_id"],
    )
    op.create_index(
        op.f("ix_snapshot_profile_source_profile_id"),
        "snapshot_profile_source",
        ["profile_id"],
    )

    op.create_table(
        "library_snapshot",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=True),
        sa.Column("bundle_id", sa.String(), nullable=False),
        sa.Column("library_id", sa.String(), nullable=False),
        sa.Column("source_providers", sa.JSON(), nullable=False),
        sa.Column("source_labels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("archive_name", sa.String(), nullable=True),
        sa.Column("archive_sha256", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(), nullable=False),
        sa.Column("verification_error", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
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
            ["profile_id"],
            ["snapshot_profile.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "archive_sha256",
            name="uq_library_snapshot_archive",
        ),
    )
    op.create_index(op.f("ix_library_snapshot_bundle_id"), "library_snapshot", ["bundle_id"])
    op.create_index(op.f("ix_library_snapshot_library_id"), "library_snapshot", ["library_id"])
    op.create_index(op.f("ix_library_snapshot_profile_id"), "library_snapshot", ["profile_id"])
    op.create_index(op.f("ix_library_snapshot_status"), "library_snapshot", ["status"])
    op.create_index(op.f("ix_library_snapshot_user_id"), "library_snapshot", ["user_id"])
    op.create_index(
        "uq_library_snapshot_active_profile",
        "library_snapshot",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text(
            "profile_id IS NOT NULL AND status IN ('pending', 'running')"
        ),
        sqlite_where=sa.text("profile_id IS NOT NULL AND status IN ('pending', 'running')"),
    )

    op.add_column(
        "migration_job",
        sa.Column(
            "source_kind",
            sa.String(),
            server_default="provider",
            nullable=False,
        ),
    )
    op.add_column(
        "migration_job",
        sa.Column("source_snapshot_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_migration_job_source_snapshot_id",
        "migration_job",
        "library_snapshot",
        ["source_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_migration_job_source_kind"), "migration_job", ["source_kind"])
    op.create_index(
        op.f("ix_migration_job_source_snapshot_id"),
        "migration_job",
        ["source_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_migration_job_source_snapshot_id"),
        table_name="migration_job",
    )
    op.drop_index(op.f("ix_migration_job_source_kind"), table_name="migration_job")
    op.drop_constraint(
        "fk_migration_job_source_snapshot_id",
        "migration_job",
        type_="foreignkey",
    )
    op.drop_column("migration_job", "source_snapshot_id")
    op.drop_column("migration_job", "source_kind")

    op.drop_index(
        "uq_library_snapshot_active_profile",
        table_name="library_snapshot",
    )
    op.drop_index(op.f("ix_library_snapshot_user_id"), table_name="library_snapshot")
    op.drop_index(op.f("ix_library_snapshot_status"), table_name="library_snapshot")
    op.drop_index(op.f("ix_library_snapshot_profile_id"), table_name="library_snapshot")
    op.drop_index(op.f("ix_library_snapshot_library_id"), table_name="library_snapshot")
    op.drop_index(op.f("ix_library_snapshot_bundle_id"), table_name="library_snapshot")
    op.drop_table("library_snapshot")
    op.drop_index(
        op.f("ix_snapshot_profile_source_profile_id"),
        table_name="snapshot_profile_source",
    )
    op.drop_index(
        op.f("ix_snapshot_profile_source_account_id"),
        table_name="snapshot_profile_source",
    )
    op.drop_table("snapshot_profile_source")
    op.drop_index(op.f("ix_snapshot_profile_user_id"), table_name="snapshot_profile")
    op.drop_table("snapshot_profile")
