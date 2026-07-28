"""Add public URL / pasted-text metadata columns to local_playlist_import.

Generalizes the local-file import lease table so public URL and pasted-text
preview snapshots can share the same table and lifecycle. Existing rows are
untouched: ``source_kind`` defaults to ``local_file`` and the new
``source_provider``/``source_label``/``source_locator``/``source_fingerprint``
columns stay nullable for local-file rows.

Revision ID: 0009_public_source_imports
Revises: 0008_local_playlist_imports
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_public_source_imports"
down_revision: str | None = "0008_local_playlist_imports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "local_playlist_import",
        sa.Column(
            "source_kind",
            sa.String(),
            nullable=False,
            server_default="local_file",
        ),
    )
    op.add_column(
        "local_playlist_import",
        sa.Column("source_provider", sa.String(), nullable=True),
    )
    op.add_column(
        "local_playlist_import",
        sa.Column("source_label", sa.String(), nullable=True),
    )
    op.add_column(
        "local_playlist_import",
        sa.Column("source_locator", sa.String(), nullable=True),
    )
    op.add_column(
        "local_playlist_import",
        sa.Column("source_fingerprint", sa.String(), nullable=True),
    )
    op.create_index(
        op.f("ix_local_playlist_import_source_provider"),
        "local_playlist_import",
        ["source_provider"],
    )
    op.create_index(
        op.f("ix_local_playlist_import_source_fingerprint"),
        "local_playlist_import",
        ["source_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_local_playlist_import_source_fingerprint"),
        table_name="local_playlist_import",
    )
    op.drop_index(
        op.f("ix_local_playlist_import_source_provider"),
        table_name="local_playlist_import",
    )
    op.drop_column("local_playlist_import", "source_fingerprint")
    op.drop_column("local_playlist_import", "source_locator")
    op.drop_column("local_playlist_import", "source_label")
    op.drop_column("local_playlist_import", "source_provider")
    op.drop_column("local_playlist_import", "source_kind")
