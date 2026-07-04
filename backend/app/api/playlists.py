"""Source playlist browsing (phase 1-2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import AuthExpired, NotFound, ProviderError, RateLimited
from app.core.migration_state import keys_from_metadata, track_keys
from app.core.models import Playlist, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


@router.get("", response_model=list[PlaylistRef])
async def list_playlists(
    provider: str,
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    target_provider: str | None = None,
    target_account_id: str | None = None,
    user_id: str = "local",
) -> list[PlaylistRef]:
    try:
        adapter = get(provider)
        credential, _ = await load_fresh_credential(
            session, account_id=account_id, adapter=adapter, provider=provider
        )
        playlists = [playlist async for playlist in adapter.iter_playlists(credential)]
        if target_provider and target_account_id:
            migrated_counts = await _migrated_counts(
                session,
                user_id=user_id,
                source_provider=provider,
                source_account_id=account_id,
                target_provider=target_provider,
                target_account_id=target_account_id,
            )
            playlists = [
                _annotate_playlist_ref(ref, migrated_counts.get(ref.id, 0))
                for ref in playlists
            ]
        await session.commit()
        return playlists
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{playlist_id}", response_model=Playlist)
async def get_playlist(
    playlist_id: str,
    provider: str,
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    target_provider: str | None = None,
    target_account_id: str | None = None,
    user_id: str = "local",
) -> Playlist:
    try:
        adapter = get(provider)
        credential, _ = await load_fresh_credential(
            session, account_id=account_id, adapter=adapter, provider=provider
        )
        playlist = await adapter.read_playlist(
            credential, PlaylistRef(id=playlist_id, name=playlist_id)
        )
        if target_provider and target_account_id:
            migrated = await _migrated_track_map(
                session,
                user_id=user_id,
                source_provider=provider,
                source_account_id=account_id,
                target_provider=target_provider,
                target_account_id=target_account_id,
                playlist_id=playlist_id,
            )
            playlist.tracks = [_annotate_track(track, migrated) for track in playlist.tracks]
        await session.commit()
        return playlist
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _is_migrated_item(item: orm.JobItem) -> bool:
    return item.status == "written" or (item.status == "skipped" and bool(item.target_uri))


async def _migrated_counts(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
) -> dict[str, int]:
    stmt = (
        select(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.source_provider == source_provider,
            orm.MigrationJob.source_account_id == source_account_id,
            orm.MigrationJob.target_provider == target_provider,
            orm.MigrationJob.target_account_id == target_account_id,
        )
    )
    counts: dict[str, set[str]] = {}
    for item in (await session.execute(stmt)).scalars():
        if not _is_migrated_item(item):
            continue
        keys = keys_from_metadata(
            item.source_metadata,
            title=item.title,
            artist=item.artist,
            album=item.album,
            duration_s=item.duration_s,
            isrc=item.isrc,
        )
        if not keys:
            continue
        counts.setdefault(item.source_playlist_id, set()).add(sorted(keys)[0])
    return {playlist_id: len(keys) for playlist_id, keys in counts.items()}


async def _migrated_track_map(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
    playlist_id: str,
) -> dict[str, tuple[str | None, str | None]]:
    stmt = (
        select(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.source_provider == source_provider,
            orm.MigrationJob.source_account_id == source_account_id,
            orm.MigrationJob.target_provider == target_provider,
            orm.MigrationJob.target_account_id == target_account_id,
            orm.JobItem.source_playlist_id == playlist_id,
        )
    )
    migrated: dict[str, tuple[str | None, str | None]] = {}
    for item in (await session.execute(stmt)).scalars():
        if not _is_migrated_item(item):
            continue
        for key in keys_from_metadata(
            item.source_metadata,
            title=item.title,
            artist=item.artist,
            album=item.album,
            duration_s=item.duration_s,
            isrc=item.isrc,
        ):
            migrated[key] = (item.target_playlist_id, item.target_uri)
    return migrated


def _annotate_playlist_ref(ref: PlaylistRef, migrated_count: int) -> PlaylistRef:
    remaining = None if ref.track_count is None else max(ref.track_count - migrated_count, 0)
    status = None
    note = None
    if migrated_count and remaining == 0:
        status = "migrated"
        note = "Migrated"
    elif migrated_count:
        status = "partial"
        note = (
            f"Partially migrated: {remaining} left"
            if remaining is not None
            else "Partially migrated"
        )
    return ref.model_copy(
        update={
            "migration_status": status,
            "migrated_track_count": migrated_count,
            "remaining_track_count": remaining,
            "migration_note": note,
        }
    )


def _annotate_track(track, migrated: dict[str, tuple[str | None, str | None]]):
    for key in track_keys(track):
        found = migrated.get(key)
        if found:
            target_playlist_id, target_uri = found
            return track.model_copy(
                update={
                    "migration_status": "migrated",
                    "migrated_target_playlist_id": target_playlist_id,
                    "migrated_target_uri": target_uri,
                }
            )
    return track.model_copy(update={"migration_status": "pending"})
