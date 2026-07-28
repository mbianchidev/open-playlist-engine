"""``GET /providers`` — the capability matrix the frontend renders.

The UI uses ``can_source`` / ``can_target`` to populate the source and target
pickers and surfaces ``warning`` inline.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.capabilities import Capability
from app.core.registry import all_adapters
from app.core.sync import mirror_unavailable_reason
from app.imports import LOCAL_FILE_PROVIDER

router = APIRouter(prefix="/api", tags=["providers"])


class ProviderView(BaseModel):
    name: str
    display_name: str
    auth_kind: str
    official: bool
    stability: str
    has_isrc: bool
    can_source: bool
    can_target: bool
    can_mirror: bool
    mirror_unavailable_reason: str | None = None
    can_unfollow_playlist: bool
    can_delete_playlist: bool
    can_remove_tracks: bool
    max_remove_batch: int
    saved_albums: LibraryCapabilityView
    followed_artists: ArtistCapabilityView
    warning: str | None = None


class LibraryCapabilityView(BaseModel):
    read: bool
    write: bool


class ArtistCapabilityView(LibraryCapabilityView):
    semantics: str | None = None


@router.get("/providers", response_model=list[ProviderView])
async def list_providers() -> list[ProviderView]:
    views: list[ProviderView] = [
        ProviderView(
            name=LOCAL_FILE_PROVIDER,
            display_name="Local playlist file",
            auth_kind="upload",
            official=True,
            stability="stable",
            has_isrc=True,
            can_source=True,
            can_target=False,
            can_mirror=False,
            mirror_unavailable_reason="Local files are available for one-time playlist migrations.",
            can_unfollow_playlist=False,
            can_delete_playlist=False,
            can_remove_tracks=False,
            max_remove_batch=0,
            saved_albums=LibraryCapabilityView(read=False, write=False),
            followed_artists=ArtistCapabilityView(read=False, write=False),
        )
    ]
    for adapter in all_adapters():
        info = adapter.info
        caps = info.capabilities
        mirror_reason = mirror_unavailable_reason(adapter)
        views.append(
            ProviderView(
                name=info.name,
                display_name=info.display_name,
                auth_kind=info.auth_kind.value,
                official=caps.official,
                stability=caps.stability.value,
                has_isrc=caps.has_isrc,
                can_source=any(
                    caps.can(capability)
                    for capability in (
                        Capability.READ_TRACKS,
                        Capability.READ_SAVED_ALBUMS,
                        Capability.READ_FOLLOWED_ARTISTS,
                    )
                ),
                can_target=(
                    caps.can(Capability.CREATE_PLAYLIST)
                    and caps.can(Capability.ADD_TRACKS)
                )
                or caps.can(Capability.WRITE_SAVED_ALBUMS)
                or caps.can(Capability.WRITE_FOLLOWED_ARTISTS),
                can_unfollow_playlist=caps.can(Capability.UNFOLLOW_PLAYLIST),
                can_delete_playlist=caps.can(Capability.DELETE_PLAYLIST),
                can_remove_tracks=caps.can(Capability.REMOVE_TRACKS),
                max_remove_batch=caps.max_remove_batch,
                saved_albums=LibraryCapabilityView(
                    read=caps.can(Capability.READ_SAVED_ALBUMS),
                    write=caps.can(Capability.WRITE_SAVED_ALBUMS),
                ),
                followed_artists=ArtistCapabilityView(
                    read=caps.can(Capability.READ_FOLLOWED_ARTISTS),
                    write=caps.can(Capability.WRITE_FOLLOWED_ARTISTS),
                    semantics=(
                        info.artist_collection_semantics.value
                        if info.artist_collection_semantics
                        else None
                    ),
                ),
                can_mirror=mirror_reason is None,
                mirror_unavailable_reason=mirror_reason,
                warning=caps.warning,
            )
        )
    return views
