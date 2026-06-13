"""Alembic environment.

Uses the same DSN as the app (psycopg3 works in sync mode for migrations) and
autogenerates from ``app.db.models`` via ``Base.metadata``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

import app.db.models  # noqa: F401  (populate metadata)
from app.db.base import Base
from app.settings import get_settings

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
