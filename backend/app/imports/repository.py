from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as orm


class ImportedPlaylistNotFound(Exception):
    pass


async def load_imported_playlist(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
) -> orm.ImportedPlaylist:
    row = (
        await session.execute(
            select(orm.ImportedPlaylist).where(
                orm.ImportedPlaylist.id == import_id,
                orm.ImportedPlaylist.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ImportedPlaylistNotFound(import_id)
    return row
