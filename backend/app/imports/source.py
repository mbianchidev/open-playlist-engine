from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import ProviderAdapter, ProviderCredential
from app.core.capabilities import Capability
from app.core.migration_state import track_selected
from app.core.models import Playlist, PlaylistRef
from app.core.registry import get
from app.db.repositories import load_fresh_credential
from app.imports import LOCAL_FILE_PROVIDER
from app.imports.service import (
    LocalImportStateError,
    load_import_for_job,
    load_preview_import,
    selected_import_playlists,
)
from app.settings import Settings


@dataclass
class MigrationSource:
    provider: str
    display_name: str
    adapter: ProviderAdapter | None = None
    credential: ProviderCredential | None = None
    local_playlists: dict[str, Playlist] = field(default_factory=dict)

    @property
    def can_read_tracks(self) -> bool:
        return self.adapter is None or self.adapter.info.capabilities.can(Capability.READ_TRACKS)

    async def read_playlist(self, playlist_id: str) -> Playlist:
        if self.adapter is None:
            playlist = self.local_playlists.get(playlist_id)
            if playlist is None:
                raise LocalImportStateError(
                    f"Playlist '{playlist_id}' is not part of this local import.",
                    code="unknown_playlist",
                )
            return playlist
        if self.credential is None:
            raise RuntimeError(f"source credential missing for provider '{self.provider}'")
        return await self.adapter.read_playlist(
            self.credential,
            PlaylistRef(id=playlist_id, name=playlist_id),
        )

    async def selected_playlists(
        self,
        *,
        playlist_ids: list[str],
        track_filters: dict[str, list[str]],
    ) -> dict[str, Playlist]:
        if self.adapter is None:
            selected: dict[str, Playlist] = {}
            for playlist_id in playlist_ids:
                playlist = await self.read_playlist(playlist_id)
                wanted = set(track_filters.get(playlist_id) or [])
                selected[playlist_id] = playlist.model_copy(
                    update={
                        "tracks": [
                            track for track in playlist.tracks if track_selected(track, wanted)
                        ]
                    }
                )
            return selected
        selected = {}
        for playlist_id in playlist_ids:
            playlist = await self.read_playlist(playlist_id)
            wanted = set(track_filters.get(playlist_id) or [])
            selected[playlist_id] = playlist.model_copy(
                update={
                    "tracks": [
                        track for track in playlist.tracks if track_selected(track, wanted)
                    ]
                }
            )
        return selected


async def open_migration_source(
    session: AsyncSession,
    *,
    provider: str,
    account_id: str,
    user_id: str,
    settings: Settings,
    job_id: str | None = None,
) -> MigrationSource:
    if provider == LOCAL_FILE_PROVIDER:
        record = (
            await load_import_for_job(
                session,
                import_id=account_id,
                user_id=user_id,
                job_id=job_id,
                settings=settings,
            )
            if job_id
            else await load_preview_import(
                session,
                import_id=account_id,
                user_id=user_id,
            )
        )
        playlists = selected_import_playlists(
            record,
            playlist_ids=[
                str(value.get("id"))
                for value in record.playlists or []
                if isinstance(value, dict) and value.get("id")
            ],
            track_filters={},
        )
        return MigrationSource(
            provider=provider,
            display_name="Local file",
            local_playlists=playlists,
        )

    adapter = get(provider)
    credential, _ = await load_fresh_credential(
        session,
        account_id=account_id,
        adapter=adapter,
        provider=provider,
    )
    return MigrationSource(
        provider=provider,
        display_name=adapter.info.display_name,
        adapter=adapter,
        credential=credential,
    )
