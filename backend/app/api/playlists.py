"""Source playlist browsing (phase 1-2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import AccessDenied, AuthExpired, NotFound, ProviderError, RateLimited
from app.core.migration_state import keys_from_metadata, track_keys
from app.core.models import Playlist, PlaylistKind, PlaylistRef, Track
from app.core.registry import get
from app.db import models as orm
from app.db.account_scope import provider_account_history
from app.db.base import get_session
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential
from app.providers.spotify.adapter import SPOTIFY_SAVED_TRACKS_PLAYLIST_ID

router = APIRouter(prefix="/api/playlists", tags=["playlists"])
_SPOTIFY_SAVED_TRACKS_NAME = "Liked Songs"


@dataclass
class _PlaylistMigrationSummary:
    migrated_keys: set[str] = field(default_factory=set)
    skipped_keys: set[str] = field(default_factory=set)
    completed_full_playlist: bool = False
    completed_item_count: int = 0

    @property
    def migrated_count(self) -> int:
        return len(self.migrated_keys)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_keys - self.migrated_keys)

    @property
    def resolved_count(self) -> int:
        return max(len(self.migrated_keys | self.skipped_keys), self.completed_item_count)


@router.get("", response_model=list[PlaylistRef])
async def list_playlists(
    provider: str,
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    target_provider: str | None = None,
    target_account_id: str | None = None,
    refresh: bool = False,
    user_id: str = "local",
) -> list[PlaylistRef]:
    try:
        adapter = get(provider)
        credential, _ = await load_fresh_credential(
            session, account_id=account_id, adapter=adapter, provider=provider
        )
        playlists = await _playlist_refs(
            session,
            adapter=adapter,
            credential=credential,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            refresh=refresh,
        )
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
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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
    refresh: bool = False,
    user_id: str = "local",
) -> Playlist:
    try:
        adapter = get(provider)
        credential, _ = await load_fresh_credential(
            session, account_id=account_id, adapter=adapter, provider=provider
        )
        playlist = await _playlist_detail(
            session,
            adapter=adapter,
            credential=credential,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            playlist_id=playlist_id,
            refresh=refresh,
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
            await _persist_discovered_full_migration(
                session,
                user_id=user_id,
                source_provider=provider,
                source_account_id=account_id,
                target_provider=target_provider,
                target_account_id=target_account_id,
                playlist_id=playlist_id,
                playlist=playlist,
            )
        await session.commit()
        return playlist
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


def _uses_snapshot_cache(provider: str) -> bool:
    return provider == "spotify"


async def _playlist_refs(
    session: AsyncSession,
    *,
    adapter,
    credential,
    user_id: str,
    provider: str,
    account_id: str,
    refresh: bool,
) -> list[PlaylistRef]:
    if _uses_snapshot_cache(provider) and not refresh:
        cached = await _cached_playlist_refs(
            session, user_id=user_id, provider=provider, account_id=account_id
        )
        if cached:
            return cached
    playlists = [playlist async for playlist in adapter.iter_playlists(credential)]
    if _uses_snapshot_cache(provider):
        await _persist_playlist_refs(
            session,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            refs=playlists,
        )
    return playlists


async def _playlist_detail(
    session: AsyncSession,
    *,
    adapter,
    credential,
    user_id: str,
    provider: str,
    account_id: str,
    playlist_id: str,
    refresh: bool,
) -> Playlist:
    cached_ref = (
        await _cached_playlist_ref(
            session,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            playlist_id=playlist_id,
        )
        if _uses_snapshot_cache(provider)
        else None
    )
    if _uses_snapshot_cache(provider) and cached_ref and not refresh:
        cached = await _cached_playlist_tracks(
            session,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            playlist_id=playlist_id,
            snapshot_id=cached_ref.snapshot_id,
        )
        if cached is not None:
            return cached

    ref = cached_ref or PlaylistRef(id=playlist_id, name=playlist_id)
    playlist = await adapter.read_playlist(credential, ref)
    if _uses_snapshot_cache(provider):
        await _persist_playlist_refs(
            session,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            refs=[
                PlaylistRef(
                    id=playlist.id or playlist_id,
                    name=playlist.name,
                    track_count=len(playlist.tracks),
                    owner_id=playlist.owner_id,
                    owner_name=playlist.owner_name,
                    is_owned=playlist.is_owned,
                    is_followed=playlist.is_followed,
                    collaborative=playlist.collaborative,
                    snapshot_id=playlist.snapshot_id or ref.snapshot_id,
                    tracks_href=ref.tracks_href,
                    created_at=playlist.created_at,
                    updated_at=playlist.updated_at,
                )
            ],
        )
        await _persist_playlist_tracks(
            session,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            playlist_id=playlist_id,
            playlist=playlist,
            fallback_snapshot_id=ref.snapshot_id,
        )
    return playlist


async def _cached_playlist_refs(
    session: AsyncSession, *, user_id: str, provider: str, account_id: str
) -> list[PlaylistRef]:
    stmt = (
        select(orm.CachedPlaylistRef)
        .where(
            orm.CachedPlaylistRef.user_id == user_id,
            orm.CachedPlaylistRef.provider == provider,
            orm.CachedPlaylistRef.account_id == account_id,
        )
        .order_by(orm.CachedPlaylistRef.name.asc())
    )
    refs = [_playlist_ref_from_cache(row) for row in (await session.execute(stmt)).scalars()]
    if provider == "spotify" and all(
        ref.id != SPOTIFY_SAVED_TRACKS_PLAYLIST_ID for ref in refs
    ):
        refs.append(
            PlaylistRef(
                id=SPOTIFY_SAVED_TRACKS_PLAYLIST_ID,
                name=_SPOTIFY_SAVED_TRACKS_NAME,
                is_owned=True,
                is_followed=False,
                collaborative=False,
                tracks_href="/me/tracks",
                migration_note="Load songs to cache your Spotify Liked Songs",
                kind=PlaylistKind.LIKED_TRACKS,
            )
        )
    return refs


async def _cached_playlist_ref(
    session: AsyncSession, *, user_id: str, provider: str, account_id: str, playlist_id: str
) -> PlaylistRef | None:
    stmt = select(orm.CachedPlaylistRef).where(
        orm.CachedPlaylistRef.user_id == user_id,
        orm.CachedPlaylistRef.provider == provider,
        orm.CachedPlaylistRef.account_id == account_id,
        orm.CachedPlaylistRef.playlist_id == playlist_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return _playlist_ref_from_cache(row) if row else None


def _playlist_ref_from_cache(row: orm.CachedPlaylistRef) -> PlaylistRef:
    return PlaylistRef(
        id=row.playlist_id,
        name=row.name,
        track_count=row.track_count,
        owner_id=row.owner_id,
        owner_name=row.owner_name,
        is_owned=row.is_owned,
        is_followed=row.is_followed,
        collaborative=row.collaborative,
        snapshot_id=row.snapshot_id,
        tracks_href=row.tracks_href,
        created_at=row.provider_created_at,
        updated_at=row.provider_updated_at,
        kind=_cached_playlist_kind(row.provider, row.playlist_id),
    )


async def _persist_playlist_refs(
    session: AsyncSession,
    *,
    user_id: str,
    provider: str,
    account_id: str,
    refs: list[PlaylistRef],
) -> None:
    for ref in refs:
        stmt = select(orm.CachedPlaylistRef).where(
            orm.CachedPlaylistRef.user_id == user_id,
            orm.CachedPlaylistRef.provider == provider,
            orm.CachedPlaylistRef.account_id == account_id,
            orm.CachedPlaylistRef.playlist_id == ref.id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            session.add(
                orm.CachedPlaylistRef(
                    user_id=user_id,
                    provider=provider,
                    account_id=account_id,
                    playlist_id=ref.id,
                    name=ref.name,
                    track_count=ref.track_count,
                    owner_id=ref.owner_id,
                    owner_name=ref.owner_name,
                    is_owned=ref.is_owned,
                    is_followed=ref.is_followed,
                    collaborative=ref.collaborative,
                    snapshot_id=ref.snapshot_id,
                    tracks_href=ref.tracks_href,
                    provider_created_at=ref.created_at,
                    provider_updated_at=ref.updated_at,
                )
            )
            continue
        row.name = ref.name
        row.track_count = ref.track_count
        row.owner_id = ref.owner_id
        row.owner_name = ref.owner_name
        row.is_owned = ref.is_owned
        row.is_followed = ref.is_followed
        row.collaborative = ref.collaborative
        row.snapshot_id = ref.snapshot_id
        row.tracks_href = ref.tracks_href
        row.provider_created_at = ref.created_at
        row.provider_updated_at = ref.updated_at
    await session.flush()


async def _cached_playlist_tracks(
    session: AsyncSession,
    *,
    user_id: str,
    provider: str,
    account_id: str,
    playlist_id: str,
    snapshot_id: str | None,
) -> Playlist | None:
    stmt = select(orm.CachedPlaylistTracks).where(
        orm.CachedPlaylistTracks.user_id == user_id,
        orm.CachedPlaylistTracks.provider == provider,
        orm.CachedPlaylistTracks.account_id == account_id,
        orm.CachedPlaylistTracks.playlist_id == playlist_id,
        orm.CachedPlaylistTracks.snapshot_id == snapshot_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return Playlist(
        id=row.playlist_id,
        name=row.name,
        owner_id=row.owner_id,
        snapshot_id=row.snapshot_id,
        tracks=[Track.model_validate(track) for track in row.tracks],
        kind=_cached_playlist_kind(row.provider, row.playlist_id),
    )


def _cached_playlist_kind(provider: str, playlist_id: str) -> PlaylistKind:
    if provider == "spotify" and playlist_id == SPOTIFY_SAVED_TRACKS_PLAYLIST_ID:
        return PlaylistKind.LIKED_TRACKS
    return PlaylistKind.STANDARD


async def _persist_playlist_tracks(
    session: AsyncSession,
    *,
    user_id: str,
    provider: str,
    account_id: str,
    playlist_id: str,
    playlist: Playlist,
    fallback_snapshot_id: str | None,
) -> None:
    stmt = select(orm.CachedPlaylistTracks).where(
        orm.CachedPlaylistTracks.user_id == user_id,
        orm.CachedPlaylistTracks.provider == provider,
        orm.CachedPlaylistTracks.account_id == account_id,
        orm.CachedPlaylistTracks.playlist_id == playlist_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    snapshot_id = playlist.snapshot_id or fallback_snapshot_id
    tracks = [track.model_dump(mode="json") for track in playlist.tracks]
    if row is None:
        session.add(
            orm.CachedPlaylistTracks(
                user_id=user_id,
                provider=provider,
                account_id=account_id,
                playlist_id=playlist_id,
                snapshot_id=snapshot_id,
                name=playlist.name,
                owner_id=playlist.owner_id,
                tracks=tracks,
            )
        )
        await session.flush()
        return
    row.snapshot_id = snapshot_id
    row.name = playlist.name
    row.owner_id = playlist.owner_id
    row.tracks = tracks
    await session.flush()


def _is_migrated_item(item: orm.JobItem) -> bool:
    return item.status == "written" or (item.status == "skipped" and bool(item.target_uri))


def _is_real_skipped_item(item: orm.JobItem) -> bool:
    return item.status == "skipped" and not item.target_uri


def _is_resolved_item(item: orm.JobItem) -> bool:
    return _is_migrated_item(item) or _is_real_skipped_item(item)


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
        keys = _source_item_keys(item)
        if _is_real_skipped_item(item) and keys:
            summary.skipped_keys.add(sorted(keys)[0])
        if not _is_migrated_item(item):
            job_items.setdefault((job.id, item.source_playlist_id), (job, []))[1].append(item)
            continue
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
            and all(_is_resolved_item(item) for item in items)
        ):
            summary = summaries.setdefault(playlist_id, _PlaylistMigrationSummary())
            summary.completed_full_playlist = True
            summary.completed_item_count = max(summary.completed_item_count, len(items))
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


async def _persist_discovered_full_migration(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
    playlist_id: str,
    playlist: Playlist,
) -> None:
    if not playlist.tracks or any(
        track.migration_status != "migrated" for track in playlist.tracks
    ):
        return
    if await _completed_full_playlist_exists(
        session,
        user_id=user_id,
        source_provider=source_provider,
        source_account_id=source_account_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
        playlist_id=playlist_id,
    ):
        return

    job = orm.MigrationJob(
        user_id=user_id,
        source_provider=source_provider,
        target_provider=target_provider,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        selection={"playlist_ids": [playlist_id], "tracks": {}},
        status="done",
    )
    session.add(job)
    await session.flush()
    for fallback_position, track in enumerate(playlist.tracks):
        session.add(
            orm.JobItem(
                job_id=job.id,
                source_playlist_id=playlist_id,
                source_playlist_name=playlist.name,
                target_playlist_id=track.migrated_target_playlist_id,
                position=track.position if track.position is not None else fallback_position,
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration_s=track.duration_s,
                release_year=track.release_year,
                explicit=track.explicit,
                isrc=track.isrc,
                source_metadata=track.model_dump(mode="json"),
                target_uri=track.migrated_target_uri,
                status="written",
                reason="already migrated when playlist was discovered",
            )
        )
    await session.flush()
    job.total = len(playlist.tracks)
    job.done = len(playlist.tracks)
    job.failed = 0


async def _completed_full_playlist_exists(
    session: AsyncSession,
    *,
    user_id: str,
    source_provider: str,
    source_account_id: str,
    target_provider: str,
    target_account_id: str,
    playlist_id: str,
) -> bool:
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
            orm.MigrationJob.status == "done",
            orm.JobItem.source_playlist_id == playlist_id,
        )
    )
    job_items: dict[str, tuple[orm.MigrationJob, list[orm.JobItem]]] = {}
    for item, job in (await session.execute(stmt)).all():
        job_items.setdefault(job.id, (job, []))[1].append(item)
    return any(
        items
        and _job_selected_full_playlist(job, playlist_id)
        and all(_is_resolved_item(item) for item in items)
        for job, items in job_items.values()
    )


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
    skipped_count = summary.skipped_count if summary else 0
    resolved_count = summary.resolved_count if summary else 0
    remaining = None if ref.track_count is None else max(ref.track_count - resolved_count, 0)
    full_without_known_total = bool(
        summary and summary.completed_full_playlist and ref.track_count is None
    )
    fully_resolved = bool(
        summary and (full_without_known_total or (ref.track_count is not None and remaining == 0))
    )
    delta_count = (
        remaining
        if summary
        and summary.completed_full_playlist
        and ref.track_count is not None
        and remaining > 0
        else 0
    )
    status = None
    note = ref.migration_note
    if fully_resolved:
        status = "migrated"
        note = "Migrated"
        remaining = None if ref.track_count is None else 0
    elif delta_count:
        status = "delta"
        note = f"Delta available: {delta_count} new"
    elif skipped_count:
        status = "partial"
        note = (
            f"Partially migrated: {remaining} left"
            if remaining is not None
            else f"Partially migrated: {skipped_count} skipped"
        )
    elif migrated_count and remaining == 0:
        status = "migrated"
        note = "Migrated"
        remaining = None if ref.track_count is None else 0
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
