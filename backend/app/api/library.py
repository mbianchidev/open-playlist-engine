"""Browse explicit saved-album and followed/favorite-artist collections."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    FollowedArtistReader,
    FollowedArtistWriter,
    NotFound,
    ProviderCredential,
    ProviderError,
    RateLimited,
    SavedAlbumReader,
    SavedAlbumWriter,
    Unsupported,
)
from app.core.capabilities import Capability
from app.core.models import Album, Artist
from app.core.registry import get
from app.db.base import get_session
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential

router = APIRouter(prefix="/api/library", tags=["library"])


class SavedAlbumsView(BaseModel):
    source_supported: bool
    target_supported: bool
    count: int = 0
    items: list[Album] = Field(default_factory=list)
    source_limitation: str | None = None
    target_limitation: str | None = None


class FollowedArtistsView(BaseModel):
    source_supported: bool
    target_supported: bool
    source_semantics: str | None = None
    target_semantics: str | None = None
    count: int = 0
    items: list[Artist] = Field(default_factory=list)
    source_limitation: str | None = None
    target_limitation: str | None = None


class LibraryView(BaseModel):
    saved_albums: SavedAlbumsView
    followed_artists: FollowedArtistsView


@router.get("", response_model=LibraryView)
async def get_library(
    provider: str,
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    target_provider: str | None = None,
    target_account_id: str | None = None,
) -> LibraryView:
    del user_id
    try:
        source = get(provider)
        source_cred, _ = await load_fresh_credential(
            session,
            account_id=account_id,
            adapter=source,
            provider=provider,
        )
        target = None
        target_cred = None
        if target_provider and target_account_id:
            target = get(target_provider)
            target_cred, _ = await load_fresh_credential(
                session,
                account_id=target_account_id,
                adapter=target,
                provider=target_provider,
            )
        view = await _build_library_view(source, source_cred, target, target_cred)
        await session.commit()
        return view
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _build_library_view(
    source,
    source_cred: ProviderCredential,
    target,
    target_cred: ProviderCredential | None,
) -> LibraryView:
    album_source = source if isinstance(source, SavedAlbumReader) else None
    album_target = target if isinstance(target, SavedAlbumWriter) else None
    artist_source = source if isinstance(source, FollowedArtistReader) else None
    artist_target = target if isinstance(target, FollowedArtistWriter) else None

    album_source_supported = bool(
        album_source
        and source.info.capabilities.can(Capability.READ_SAVED_ALBUMS)
    )
    album_target_supported = bool(
        album_target
        and target
        and target.info.capabilities.can(Capability.WRITE_SAVED_ALBUMS)
    )
    album_source_limitation = None
    album_target_limitation = None
    albums: list[Album] = []
    if album_source_supported and album_source:
        try:
            source.info.require_saved_albums_source(source_cred)
        except (AccessDenied, Unsupported) as exc:
            album_source_limitation = str(exc)
        else:
            albums = [album async for album in album_source.iter_saved_albums(source_cred)]
    else:
        album_source_limitation = f"{source.info.display_name} cannot read saved albums"
    if album_target_supported and target and target_cred:
        try:
            target.info.require_saved_albums_target(target_cred)
        except (AccessDenied, Unsupported) as exc:
            album_target_limitation = str(exc)
    elif target is not None:
        album_target_limitation = f"{target.info.display_name} cannot write saved albums"

    artist_source_supported = bool(
        artist_source
        and source.info.capabilities.can(Capability.READ_FOLLOWED_ARTISTS)
    )
    artist_target_supported = bool(
        artist_target
        and target
        and target.info.capabilities.can(Capability.WRITE_FOLLOWED_ARTISTS)
    )
    artist_source_limitation = None
    artist_target_limitation = None
    artists: list[Artist] = []
    if artist_source_supported and artist_source:
        try:
            source.info.require_followed_artists_source(source_cred)
        except (AccessDenied, Unsupported) as exc:
            artist_source_limitation = str(exc)
        else:
            artists = [
                artist async for artist in artist_source.iter_followed_artists(source_cred)
            ]
    else:
        artist_source_limitation = (
            f"{source.info.display_name} cannot read followed or favorite artists"
        )
    if artist_target_supported and target and target_cred:
        try:
            target.info.require_followed_artists_target(target_cred)
        except (AccessDenied, Unsupported) as exc:
            artist_target_limitation = str(exc)
    elif target is not None:
        artist_target_limitation = (
            f"{target.info.display_name} cannot write followed or favorite artists"
        )

    return LibraryView(
        saved_albums=SavedAlbumsView(
            source_supported=album_source_supported,
            target_supported=album_target_supported,
            count=len(albums),
            items=albums,
            source_limitation=album_source_limitation,
            target_limitation=album_target_limitation,
        ),
        followed_artists=FollowedArtistsView(
            source_supported=artist_source_supported,
            target_supported=artist_target_supported,
            source_semantics=(
                source.info.artist_collection_semantics.value
                if source.info.artist_collection_semantics
                else None
            ),
            target_semantics=(
                target.info.artist_collection_semantics.value
                if target and target.info.artist_collection_semantics
                else None
            ),
            count=len(artists),
            items=artists,
            source_limitation=artist_source_limitation,
            target_limitation=artist_target_limitation,
        ),
    )
