"""``GET /providers`` — the capability matrix the frontend renders.

The UI uses ``can_source`` / ``can_target`` to populate the source and target
pickers and surfaces ``warning`` inline.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.capabilities import Capability
from app.core.registry import all_info

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
    views: list[ProviderView] = []
    for info in all_info():
        caps = info.capabilities
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
                warning=caps.warning,
            )
        )
    return views
