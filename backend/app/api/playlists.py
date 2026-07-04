"""Source playlist browsing (phase 1-2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import AuthExpired, NotFound, ProviderError, RateLimited
from app.core.migration_state import keys_from_metadata, track_keys
from app.core.models import Playlist, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.account_scope import provider_account_history
from app.db.base import get_session
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


@dataclass
class _PlaylistMigrationSummary:
    migrated_keys: set[str] = field(default_factory=set)
    completed_full_playlist: bool = False

    @property
    def migrated_count(self) -> int:
        return len(self.migrated_keys)


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
            migration_summaries = await _migration_summaries(
                session,
                user_id=user_id,
                source_provider=provider,
                source_account_id=account_id,
                target_provider=target_provider,
                target_account_id=target_account_id,
            )
            playlists = [
                _annotate_playlist_ref(ref, migration_summaries.get(ref.id))
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


async def _migration_summaries(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
) -> dict[str, _PlaylistMigrationSummary]:
    stmt = (
        select(orm.JobItem, orm.MigrationJob)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.source_provider == source_provider,
            provider_account_history(
                orm.MigrationJob.source_account_id,
                current_account_id=source_account_id,
                user_id=user_id,
                provider=source_provider,
            ),
            orm.MigrationJob.target_provider == target_provider,
            provider_account_history(
                orm.MigrationJob.target_account_id,
                current_account_id=target_account_id,
                user_id=user_id,
                provider=target_provider,
            ),
        )
    )
    summaries: dict[str, _PlaylistMigrationSummary] = {}
    job_items: dict[tuple[str, str], tuple[orm.MigrationJob, list[orm.JobItem]]] = {}
    for item, job in (await session.execute(stmt)).all():
        summary = summaries.setdefault(item.source_playlist_id, _PlaylistMigrationSummary())
        if not _is_migrated_item(item):
            job_items.setdefault((job.id, item.source_playlist_id), (job, []))[1].append(item)
            continue
        keys = _source_item_keys(item)
        if not keys:
            job_items.setdefault((job.id, item.source_playlist_id), (job, []))[1].append(item)
            continue
        summary.migrated_keys.add(sorted(keys)[0])
        job_items.setdefault((job.id, item.source_playlist_id), (job, []))[1].append(item)

    for (_, playlist_id), (job, items) in job_items.items():
        if (
            items
            and job.status == "done"
            and _job_selected_full_playlist(job, playlist_id)
            and all(_is_migrated_item(item) for item in items)
        ):
            summary = summaries.setdefault(playlist_id, _PlaylistMigrationSummary())
            summary.completed_full_playlist = True
    return summaries


def _source_item_keys(item: orm.JobItem) -> set[str]:
    return keys_from_metadata(
        item.source_metadata,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        isrc=item.isrc,
    )


def _job_selected_full_playlist(job: orm.MigrationJob, playlist_id: str) -> bool:
    selection = job.selection or {}
    tracks = selection.get("tracks") if isinstance(selection, dict) else None
    if not isinstance(tracks, dict):
        return True
    return not tracks.get(playlist_id)


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
            provider_account_history(
                orm.MigrationJob.source_account_id,
                current_account_id=source_account_id,
                user_id=user_id,
                provider=source_provider,
            ),
            orm.MigrationJob.target_provider == target_provider,
            provider_account_history(
                orm.MigrationJob.target_account_id,
                current_account_id=target_account_id,
                user_id=user_id,
                provider=target_provider,
            ),
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


def _annotate_playlist_ref(
    ref: PlaylistRef, summary: _PlaylistMigrationSummary | None
) -> PlaylistRef:
    migrated_count = summary.migrated_count if summary else 0
    remaining = None if ref.track_count is None else max(ref.track_count - migrated_count, 0)
    status = None
    note = None
    full_migration = bool(summary and summary.completed_full_playlist)
    if migrated_count and (remaining == 0 or (remaining is None and full_migration)):
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
