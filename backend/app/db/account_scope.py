"""Query helpers for migration history tied to provider accounts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.db import models as orm


def provider_account_history(
    account_id_column: Any, *, current_account_id: str, user_id: str, provider: str
) -> ColumnElement[bool]:
    """Match current, previous same-provider, and deleted account IDs for one user."""
    return or_(
        account_id_column == current_account_id,
        select(orm.ProviderAccount.id)
        .where(
            orm.ProviderAccount.id == account_id_column,
            orm.ProviderAccount.user_id == user_id,
            orm.ProviderAccount.provider == provider,
        )
        .exists(),
        ~select(orm.ProviderAccount.id)
        .where(
            orm.ProviderAccount.id == account_id_column,
        )
        .exists(),
    )
