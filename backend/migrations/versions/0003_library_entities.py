"""Add explicit album and artist migration items.

Revision ID: 0003_library_entities
Revises: 0002_playlist_read_cache
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_library_entities"
down_revision: str | None = "0002_playlist_read_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_item",
        sa.Column("entity_type", sa.String(), server_default="track", nullable=False),
    )
    op.add_column("job_item", sa.Column("source_entity_id", sa.String(), nullable=True))
    op.add_column("job_item", sa.Column("source_entity_name", sa.String(), nullable=True))
    op.add_column("job_item", sa.Column("target_entity_id", sa.String(), nullable=True))
    op.alter_column("job_item", "source_playlist_id", existing_type=sa.String(), nullable=True)
    op.create_index(op.f("ix_job_item_entity_type"), "job_item", ["entity_type"])


def downgrade() -> None:
    op.drop_index(op.f("ix_job_item_entity_type"), table_name="job_item")
    op.execute("DELETE FROM job_item WHERE entity_type <> 'track'")
    op.execute(
        """
        UPDATE migration_job
        SET total = (
                SELECT COUNT(*) FROM job_item WHERE job_item.job_id = migration_job.id
            ),
            done = (
                SELECT COUNT(*) FROM job_item
                WHERE job_item.job_id = migration_job.id
                  AND job_item.status IN ('written', 'skipped', 'needs_review')
            ),
            failed = (
                SELECT COUNT(*) FROM job_item
                WHERE job_item.job_id = migration_job.id
                  AND job_item.status = 'failed'
            )
        """
    )
    op.alter_column("job_item", "source_playlist_id", existing_type=sa.String(), nullable=False)
    op.drop_column("job_item", "target_entity_id")
    op.drop_column("job_item", "source_entity_name")
    op.drop_column("job_item", "source_entity_id")
    op.drop_column("job_item", "entity_type")
