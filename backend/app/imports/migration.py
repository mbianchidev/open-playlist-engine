from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.migration_state import track_selected
from app.core.models import Playlist
from app.imports.repository import load_imported_playlist


class ImportSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class ImportedMigrationSource:
    import_id: str
    provider: str
    account_id: str
    label: str
    playlist: Playlist


async def load_import_source(
    session: AsyncSession,
    *,
    import_id: str,
    user_id: str,
) -> ImportedMigrationSource:
    row = await load_imported_playlist(
        session,
        import_id=import_id,
        user_id=user_id,
    )
    return ImportedMigrationSource(
        import_id=row.id,
        provider=row.source_provider,
        account_id=f"import:{row.source_provider}",
        label=row.source_label,
        playlist=Playlist.model_validate(row.playlist),
    )


def selected_import_playlists(
    source: ImportedMigrationSource,
    *,
    playlist_ids: list[str],
    track_filters: dict[str, list[str]],
) -> dict[str, Playlist]:
    playlist_id = source.playlist.id or ""
    if playlist_ids != [playlist_id]:
        raise ImportSelectionError(
            "Imported migrations must select the playlist returned by the preview."
        )
    wanted = set(track_filters.get(playlist_id) or [])
    tracks = [track for track in source.playlist.tracks if track_selected(track, wanted)]
    return {playlist_id: source.playlist.model_copy(update={"tracks": tracks})}
