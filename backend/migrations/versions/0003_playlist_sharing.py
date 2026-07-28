"""Add immutable public playlist sharing.

Revision ID: 0003_playlist_sharing
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_playlist_sharing"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_account",
        sa.Column("ephemeral_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_provider_account_ephemeral_expires_at"),
        "provider_account",
        ["ephemeral_expires_at"],
    )

    op.create_table(
        "playlist_share",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enc_token", sa.LargeBinary(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("snapshot_schema_version", sa.String(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(op.f("ix_playlist_share_owner_user_id"), "playlist_share", ["owner_user_id"])
    op.create_index(
        op.f("ix_playlist_share_token_hash"),
        "playlist_share",
        ["token_hash"],
        unique=True,
    )

    op.add_column("migration_job", sa.Column("source_share_id", sa.String(), nullable=True))
    op.add_column("migration_job", sa.Column("source_snapshot", sa.JSON(), nullable=True))
    op.create_foreign_key(
        "fk_migration_job_source_share_id_playlist_share",
        "migration_job",
        "playlist_share",
        ["source_share_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_migration_job_source_share_id"), "migration_job", ["source_share_id"]
    )

    op.create_table(
        "share_recipient_auth_state",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("share_id", sa.String(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("recipient_user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["share_id"], ["playlist_share.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_share_recipient_auth_state_expires_at"),
        "share_recipient_auth_state",
        ["expires_at"],
    )
    op.create_index(
        op.f("ix_share_recipient_auth_state_recipient_user_id"),
        "share_recipient_auth_state",
        ["recipient_user_id"],
    )
    op.create_index(
        op.f("ix_share_recipient_auth_state_share_id"),
        "share_recipient_auth_state",
        ["share_id"],
    )
    op.create_index(
        op.f("ix_share_recipient_auth_state_state_hash"),
        "share_recipient_auth_state",
        ["state_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_share_recipient_auth_state_state_hash"),
        table_name="share_recipient_auth_state",
    )
    op.drop_index(
        op.f("ix_share_recipient_auth_state_share_id"),
        table_name="share_recipient_auth_state",
    )
    op.drop_index(
        op.f("ix_share_recipient_auth_state_recipient_user_id"),
        table_name="share_recipient_auth_state",
    )
    op.drop_index(
        op.f("ix_share_recipient_auth_state_expires_at"),
        table_name="share_recipient_auth_state",
    )
    op.drop_table("share_recipient_auth_state")
    op.drop_index(op.f("ix_migration_job_source_share_id"), table_name="migration_job")
    op.drop_constraint(
        "fk_migration_job_source_share_id_playlist_share",
        "migration_job",
        type_="foreignkey",
    )
    op.drop_column("migration_job", "source_snapshot")
    op.drop_column("migration_job", "source_share_id")
    op.drop_index(op.f("ix_playlist_share_token_hash"), table_name="playlist_share")
    op.drop_index(op.f("ix_playlist_share_owner_user_id"), table_name="playlist_share")
    op.drop_table("playlist_share")
    op.drop_index(
        op.f("ix_provider_account_ephemeral_expires_at"),
        table_name="provider_account",
    )
    op.drop_column("provider_account", "ephemeral_expires_at")
