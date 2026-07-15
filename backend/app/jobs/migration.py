"""The migration pipeline: import -> match -> review -> write.

Each phase is resumable. Writes go through the operation ledger
(:class:`app.db.models.OperationLedger`): we persist intent, call the provider,
persist the observed target id/position, and on uncertain failure reconcile by
*reading* target state instead of blindly retrying a non-idempotent call.
Progress is derived from ``job_item`` rows so a disconnected client can replay.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import (
    CreatePlaylistSpec,
    FollowedArtistReader,
    FollowedArtistWriter,
    NotFound,
    ProviderAdapter,
    ProviderCredential,
    ProviderError,
    SavedAlbumReader,
    SavedAlbumWriter,
    TrackCandidate,
)
from app.core.library_match import LibraryMatchResult, LibraryMatchService
from app.core.match_service import MatchResult, MatchService
from app.core.migration_state import (
    has_track_overlap,
    keys_from_metadata,
    track_keys,
    track_selected,
    uri_keys,
)
from app.core.models import (
    Album,
    Artist,
    MigrationEntityType,
    PlaylistKind,
    PlaylistRef,
    Track,
)
from app.core.registry import get
from app.db import models as orm
from app.db.account_scope import provider_account_history
from app.db.base import get_sessionmaker
from app.db.repositories import load_fresh_credential
from app.settings import get_settings

logger = logging.getLogger(__name__)
_REVIEW_HISTORY_CONFIDENCE_BONUS = 0.10


class Phase(StrEnum):
    IMPORT = "import"
    MATCH = "match"
    REVIEW = "review"
    WRITE = "write"
    DONE = "done"


async def run_migration(ctx: dict, job_id: str) -> None:
    """Entry point invoked by the arq worker.

    Runs one durable migration job. Progress is persisted on every meaningful item
    transition so SSE clients can reconnect and reconstruct the current state.
    """
    logger.info("starting migration job_id=%s", job_id)
    async with get_sessionmaker()() as session:
        job = await session.get(orm.MigrationJob, job_id)
        if job is None:
            logger.error("migration job_id=%s not found", job_id)
            return
        try:
            await _run(session, job)
        except asyncio.CancelledError:
            await session.rollback()
            await _mark_job_failed(session, job_id, "migration cancelled or timed out")
            logger.exception("migration job_id=%s cancelled or timed out", job_id)
            raise
        except Exception as exc:
            await session.rollback()
            await _mark_job_failed(session, job_id, str(exc))
            logger.exception("migration job_id=%s failed", job_id)


async def _mark_job_failed(session: AsyncSession, job_id: str, error: str) -> None:
    job = await session.get(orm.MigrationJob, job_id)
    if job is None:
        return
    job.status = "failed"
    job.error = error
    await session.commit()


async def _run(session: AsyncSession, job: orm.MigrationJob) -> None:
    source = get(job.source_provider)
    target = get(job.target_provider)
    source_cred, _ = await load_fresh_credential(
        session, account_id=job.source_account_id, adapter=source, provider=job.source_provider
    )
    target_cred, _ = await load_fresh_credential(
        session, account_id=job.target_account_id, adapter=target, provider=job.target_provider
    )
    job.status = "running"
    job.error = None
    await session.commit()

    selection = job.selection or {}
    playlist_ids = list(selection.get("playlist_ids") or [])
    saved_album_ids = list(selection.get("saved_album_ids") or [])
    followed_artist_ids = list(selection.get("followed_artist_ids") or [])
    if not (playlist_ids or saved_album_ids or followed_artist_ids):
        raise ValueError("migration has no selected items")
    logger.info(
        "migration job_id=%s running source=%s target=%s playlist_count=%s "
        "saved_album_count=%s followed_artist_count=%s",
        job.id,
        job.source_provider,
        job.target_provider,
        len(playlist_ids),
        len(saved_album_ids),
        len(followed_artist_ids),
    )

    review_threshold = get_settings().review_confidence_threshold
    matcher = MatchService(graph=None, review_threshold=review_threshold)
    reviewed_history = await _previous_reviewed_items(
        session, job=job, review_threshold=review_threshold
    )
    for playlist_id in playlist_ids:
        logger.info(
            "migration job_id=%s reading source playlist playlist_id=%s", job.id, playlist_id
        )
        playlist = await source.read_playlist(
            source_cred, PlaylistRef(id=playlist_id, name=playlist_id)
        )
        wanted = set((selection.get("tracks") or {}).get(playlist_id) or [])
        tracks = [track for track in playlist.tracks if track_selected(track, wanted)]
        logger.info(
            "migration job_id=%s loaded source playlist playlist_id=%s name=%r "
            "total_tracks=%s selected_tracks=%s",
            job.id,
            playlist_id,
            playlist.name,
            len(playlist.tracks),
            len(tracks),
        )

        target_playlist_id = await _resolve_target_playlist(
            session,
            job=job,
            target=target,
            target_cred=target_cred,
            playlist_id=playlist_id,
            playlist_name=playlist.name,
            description=playlist.description
            or f"Migrated from {source.info.display_name} by Open Playlist Engine.",
            playlist_kind=playlist.kind,
            source_tracks=tracks,
        )
        logger.info(
            "migration job_id=%s using target playlist source_playlist_id=%s "
            "target_playlist_id=%s",
            job.id,
            playlist_id,
            target_playlist_id,
        )
        target_existing_keys = await _target_playlist_keys(target, target_cred, target_playlist_id)
        logger.info(
            "migration job_id=%s loaded target duplicate keys target_playlist_id=%s count=%s",
            job.id,
            target_playlist_id,
            len(target_existing_keys),
        )
        await session.commit()

        item_pairs = await _create_items(session, job, playlist_id, playlist.name, tracks)
        logger.info(
            "migration job_id=%s created migration items playlist_id=%s count=%s",
            job.id,
            playlist_id,
            len(item_pairs),
        )
        matched: list[orm.JobItem] = []
        for item, track in item_pairs:
            item.target_playlist_id = target_playlist_id
            if not track.is_migratable:
                item.status = "skipped"
                item.reason = track.unsupported_reason or "unsupported playlist item"
                await commit_job_counts(session, job)
                continue
            try:
                result = await matcher.resolve(track, target, target_cred)
            except ProviderError as exc:
                item.status = "failed"
                item.reason = str(exc)
                await commit_job_counts(session, job)
                continue
            result = _apply_review_history_bonus(
                reviewed_history,
                track=track,
                result=result,
                review_threshold=review_threshold,
            )
            item.confidence = result.confidence
            if result.candidate is None:
                item.status = "failed"
                item.reason = "no target match found"
                await commit_job_counts(session, job)
                continue
            item.target_uri = result.candidate.uri
            if result.needs_review:
                item.status = "needs_review"
                item.reason = result.review_reason or (
                    f"match confidence {result.confidence:.2f} below review threshold"
                )
                await commit_job_counts(session, job)
                continue
            item.status = "matched"
            matched.append(item)
            await commit_job_counts(session, job)
            await _flush_matched_chunk(
                session,
                job,
                target,
                target_cred,
                target_playlist_id,
                matched,
                existing_keys=target_existing_keys,
            )

        while matched:
            await _flush_matched_chunk(
                session,
                job,
                target,
                target_cred,
                target_playlist_id,
                matched,
                existing_keys=target_existing_keys,
                force=True,
            )
        status_counts = await _playlist_status_counts(session, job.id, playlist_id)
        logger.info(
            "migration job_id=%s finished playlist playlist_id=%s target_playlist_id=%s "
            "status_counts=%s",
            job.id,
            playlist_id,
            target_playlist_id,
            status_counts,
        )

    if saved_album_ids or followed_artist_ids:
        library_matcher = LibraryMatchService(review_threshold=review_threshold)
        if saved_album_ids:
            source_albums = _require_saved_album_reader(source)
            target_albums = _require_saved_album_writer(target)
            source.info.require_saved_albums_source(source_cred)
            target.info.require_saved_albums_target(target_cred)
            await _migrate_library_entities(
                session,
                job=job,
                source=source_albums,
                target=target_albums,
                source_cred=source_cred,
                target_cred=target_cred,
                entity_type=MigrationEntityType.ALBUM,
                entity_ids=saved_album_ids,
                read_entity=source_albums.read_saved_album,
                resolve=library_matcher.resolve_album,
            )
        if followed_artist_ids:
            source_artists = _require_followed_artist_reader(source)
            target_artists = _require_followed_artist_writer(target)
            source.info.require_followed_artists_source(source_cred)
            target.info.require_followed_artists_target(target_cred)
            await _migrate_library_entities(
                session,
                job=job,
                source=source_artists,
                target=target_artists,
                source_cred=source_cred,
                target_cred=target_cred,
                entity_type=MigrationEntityType.ARTIST,
                entity_ids=followed_artist_ids,
                read_entity=source_artists.read_followed_artist,
                resolve=library_matcher.resolve_artist,
            )

    await commit_job_counts(session, job)
    job.status = "done"
    await session.commit()
    logger.info("migration job_id=%s reached %s", job.id, Phase.DONE)


async def _flush_matched_chunk(
    session: AsyncSession,
    job: orm.MigrationJob,
    target: ProviderAdapter,
    target_cred: ProviderCredential,
    target_playlist_id: str,
    matched: list[orm.JobItem],
    *,
    existing_keys: set[str],
    force: bool = False,
) -> None:
    chunk_size = _write_chunk_size(target)
    if not matched or (not force and len(matched) < chunk_size):
        return
    chunk = matched[:chunk_size]
    del matched[:chunk_size]
    await _write_matched_items(
        session,
        job,
        target,
        target_cred,
        target_playlist_id,
        chunk,
        existing_keys=existing_keys,
    )


def _write_chunk_size(target: ProviderAdapter) -> int:
    return max(1, target.info.capabilities.max_add_batch)


async def _write_matched_items(
    session: AsyncSession,
    job: orm.MigrationJob,
    target: ProviderAdapter,
    target_cred: ProviderCredential,
    target_playlist_id: str,
    items: list[orm.JobItem],
    *,
    existing_keys: set[str],
) -> None:
    write_items = await _skip_duplicate_items(session, job, items, existing_keys=existing_keys)
    if not write_items:
        return
    try:
        results = await target.add_tracks(
            target_cred,
            target_playlist_id,
            [item.target_uri or "" for item in write_items],
        )
    except ProviderError as exc:
        for item in write_items:
            item.status = "failed"
            item.reason = str(exc)
        await commit_job_counts(session, job)
        return
    for item, result in zip(write_items, results, strict=False):
        session.add(
            orm.OperationLedger(
                job_id=job.id,
                op="add_track",
                intent={"playlist_id": target_playlist_id, "uri": result.uri},
                observed_target_id=target_playlist_id if result.ok else None,
                position=result.position,
                state="done" if result.ok else "ambiguous",
            )
        )
        if result.ok:
            item.status = "written"
            item.reason = None
            existing_keys.update(_item_target_keys(item))
        else:
            item.status = "failed"
            item.reason = result.error or "target rejected track"
        await commit_job_counts(session, job)
    for item in write_items[len(results) :]:
        item.status = "failed"
        item.reason = "target did not return a result for this track"
        await commit_job_counts(session, job)

    logger.info(
        "migration job_id=%s wrote matched chunk target_playlist_id=%s count=%s",
        job.id,
        target_playlist_id,
        len(write_items),
    )


async def _create_items(
    session: AsyncSession,
    job: orm.MigrationJob,
    playlist_id: str,
    playlist_name: str,
    tracks: list[Track],
) -> list[tuple[orm.JobItem, Track]]:
    pairs: list[tuple[orm.JobItem, Track]] = []
    for fallback_position, track in enumerate(tracks):
        item = orm.JobItem(
            job_id=job.id,
            entity_type=MigrationEntityType.TRACK,
            source_playlist_id=playlist_id,
            source_playlist_name=playlist_name,
            position=track.position if track.position is not None else fallback_position,
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration_s=track.duration_s,
            release_year=track.release_year,
            explicit=track.explicit,
            isrc=track.isrc,
            source_metadata=track.model_dump(mode="json"),
            status="pending",
        )
        session.add(item)
        pairs.append((item, track))
    await commit_job_counts(session, job)
    return pairs


LibraryEntity = Album | Artist
LibrarySource = SavedAlbumReader | FollowedArtistReader
LibraryTarget = SavedAlbumWriter | FollowedArtistWriter
LibraryReader = Callable[[ProviderCredential, str], Awaitable[LibraryEntity]]
LibraryResolver = Callable[
    [LibraryEntity, LibraryTarget, ProviderCredential],
    Awaitable[LibraryMatchResult],
]


def _require_saved_album_reader(adapter: ProviderAdapter) -> SavedAlbumReader:
    if not isinstance(adapter, SavedAlbumReader):
        raise ProviderError(f"{adapter.info.display_name} cannot read saved albums")
    return adapter


def _require_saved_album_writer(adapter: ProviderAdapter) -> SavedAlbumWriter:
    if not isinstance(adapter, SavedAlbumWriter):
        raise ProviderError(f"{adapter.info.display_name} cannot write saved albums")
    return adapter


def _require_followed_artist_reader(adapter: ProviderAdapter) -> FollowedArtistReader:
    if not isinstance(adapter, FollowedArtistReader):
        raise ProviderError(
            f"{adapter.info.display_name} cannot read followed or favorite artists"
        )
    return adapter


def _require_followed_artist_writer(adapter: ProviderAdapter) -> FollowedArtistWriter:
    if not isinstance(adapter, FollowedArtistWriter):
        raise ProviderError(
            f"{adapter.info.display_name} cannot write followed or favorite artists"
        )
    return adapter


async def _migrate_library_entities(
    session: AsyncSession,
    *,
    job: orm.MigrationJob,
    source: LibrarySource,
    target: LibraryTarget,
    source_cred: ProviderCredential,
    target_cred: ProviderCredential,
    entity_type: MigrationEntityType,
    entity_ids: list[str],
    read_entity: LibraryReader,
    resolve: LibraryResolver,
) -> None:
    items = [
        _pending_library_job_item(
            job.id,
            entity_type,
            entity_id,
            position=position,
        )
        for position, entity_id in enumerate(entity_ids)
    ]
    for item in items:
        session.add(item)
    await commit_job_counts(session, job)

    try:
        if entity_type is MigrationEntityType.ALBUM:
            source_present = await source.contains_saved_albums(source_cred, entity_ids)
        else:
            source_present = await source.contains_followed_artists(source_cred, entity_ids)
    except ProviderError as exc:
        for item in items:
            item.status = "failed"
            item.reason = str(exc)
        await commit_job_counts(session, job)
        return
    if len(source_present) != len(items):
        for item in items:
            item.status = "failed"
            item.reason = "source returned an invalid library membership response"
        await commit_job_counts(session, job)
        return

    matched: list[orm.JobItem] = []
    for item, entity_id, is_present in zip(
        items, entity_ids, source_present, strict=True
    ):
        if not is_present:
            item.status = "skipped"
            item.reason = _source_library_missing_reason(source, entity_type)
            await commit_job_counts(session, job)
            continue
        try:
            entity = await read_entity(source_cred, entity_id)
        except ProviderError as exc:
            item.status = "failed"
            item.reason = str(exc)
            await commit_job_counts(session, job)
            continue

        _populate_library_job_item(item, entity_type, entity)
        await commit_job_counts(session, job)
        try:
            result = await resolve(entity, target, target_cred)
        except ProviderError as exc:
            item.status = "failed"
            item.reason = str(exc)
            await commit_job_counts(session, job)
            continue
        item.confidence = result.confidence
        if result.candidate is None:
            item.status = "needs_review"
            item.reason = result.review_reason or "no target match found"
            await commit_job_counts(session, job)
            continue
        item.target_uri = result.candidate.uri
        item.target_entity_id = (
            result.candidate.provider_album_id
            if entity_type is MigrationEntityType.ALBUM
            else result.candidate.provider_artist_id
        )
        if result.needs_review:
            item.status = "needs_review"
            item.reason = result.review_reason or "target match requires review"
            await commit_job_counts(session, job)
            continue
        item.status = "matched"
        matched.append(item)
        await commit_job_counts(session, job)
        if len(matched) >= target.info.capabilities.max_library_batch:
            chunk = matched[: target.info.capabilities.max_library_batch]
            del matched[: target.info.capabilities.max_library_batch]
            await _write_library_items(
                session,
                job,
                target,
                target_cred,
                entity_type,
                chunk,
            )

    if matched:
        await _write_library_items(
            session,
            job,
            target,
            target_cred,
            entity_type,
            matched,
        )


def _library_job_item(
    job_id: str,
    entity_type: MigrationEntityType,
    entity: LibraryEntity,
    *,
    position: int,
) -> orm.JobItem:
    item = _pending_library_job_item(
        job_id,
        entity_type,
        entity.source_item_id or entity.id or "",
        position=position,
    )
    _populate_library_job_item(item, entity_type, entity)
    return item


def _pending_library_job_item(
    job_id: str,
    entity_type: MigrationEntityType,
    entity_id: str,
    *,
    position: int,
) -> orm.JobItem:
    return orm.JobItem(
        job_id=job_id,
        entity_type=entity_type,
        source_entity_id=entity_id,
        source_entity_name=entity_id,
        position=position,
        title=entity_id,
        artist=entity_id,
        source_metadata={"id": entity_id},
        status="pending",
    )


def _populate_library_job_item(
    item: orm.JobItem,
    entity_type: MigrationEntityType,
    entity: LibraryEntity,
) -> None:
    if entity_type is MigrationEntityType.ALBUM and isinstance(entity, Album):
        source_id = entity.source_item_id or entity.id
        name = entity.title
        artist = ", ".join(entity.artists) or "Unknown"
        release_year = (
            entity.release_date.year if entity.release_date else entity.release_year
        )
    elif entity_type is MigrationEntityType.ARTIST and isinstance(entity, Artist):
        source_id = entity.source_item_id or entity.id
        name = entity.name
        artist = entity.name
        release_year = None
    else:
        raise ValueError(f"invalid {entity_type} entity")
    item.source_entity_id = source_id
    item.source_entity_name = name
    item.title = name
    item.artist = artist
    item.release_year = release_year
    item.source_metadata = entity.model_dump(mode="json")


def _source_library_missing_reason(
    source: LibrarySource, entity_type: MigrationEntityType
) -> str:
    if entity_type is MigrationEntityType.ALBUM:
        return "album is no longer saved in the source library"
    semantics = source.info.artist_collection_semantics
    action = "favorited" if semantics and semantics.value == "favorite" else "followed"
    return f"artist is no longer {action} in the source library"


async def _write_library_items(
    session: AsyncSession,
    job: orm.MigrationJob,
    target: LibraryTarget,
    target_cred: ProviderCredential,
    entity_type: MigrationEntityType,
    items: list[orm.JobItem],
) -> None:
    uris = [item.target_uri or "" for item in items]
    try:
        if entity_type is MigrationEntityType.ALBUM:
            present = await target.contains_saved_albums(target_cred, uris)
        else:
            present = await target.contains_followed_artists(target_cred, uris)
    except ProviderError as exc:
        for item in items:
            item.status = "failed"
            item.reason = str(exc)
        await commit_job_counts(session, job)
        return
    if len(present) != len(items):
        for item in items:
            item.status = "failed"
            item.reason = "target returned an invalid library contains response"
        await commit_job_counts(session, job)
        return

    write_items = []
    for item, exists in zip(items, present, strict=True):
        if exists:
            item.status = "skipped"
            item.reason = _library_duplicate_reason(target, entity_type)
        else:
            write_items.append(item)
        await commit_job_counts(session, job)
    if not write_items:
        return

    try:
        if entity_type is MigrationEntityType.ALBUM:
            results = await target.save_albums(
                target_cred, [item.target_uri or "" for item in write_items]
            )
        else:
            results = await target.follow_artists(
                target_cred, [item.target_uri or "" for item in write_items]
            )
    except ProviderError as exc:
        for item in write_items:
            item.status = "failed"
            item.reason = str(exc)
        await commit_job_counts(session, job)
        return

    operation = (
        "save_album" if entity_type is MigrationEntityType.ALBUM else "follow_artist"
    )
    for item, result in zip(write_items, results, strict=False):
        session.add(
            orm.OperationLedger(
                job_id=job.id,
                op=operation,
                intent={
                    "entity_type": entity_type.value,
                    "source_entity_id": item.source_entity_id,
                    "uri": result.uri,
                },
                observed_target_id=item.target_entity_id if result.ok else None,
                state="done" if result.ok else "ambiguous",
            )
        )
        if result.already_present:
            item.status = "skipped"
            item.reason = _library_duplicate_reason(target, entity_type)
        else:
            item.status = "written" if result.ok else "failed"
            item.reason = (
                None if result.ok else result.error or "target rejected library item"
            )
        await commit_job_counts(session, job)
    for item in write_items[len(results) :]:
        item.status = "failed"
        item.reason = "target did not return a result for this library item"
        await commit_job_counts(session, job)


def _library_duplicate_reason(
    target: LibraryTarget, entity_type: MigrationEntityType
) -> str:
    if entity_type is MigrationEntityType.ALBUM:
        return "album already saved in target library"
    semantics = target.info.artist_collection_semantics
    action = "favorited" if semantics and semantics.value == "favorite" else "followed"
    return f"artist already {action} in target library"


async def _resolve_target_playlist(
    session: AsyncSession,
    *,
    job: orm.MigrationJob,
    target: ProviderAdapter,
    target_cred: ProviderCredential,
    playlist_id: str,
    playlist_name: str,
    description: str,
    playlist_kind: PlaylistKind,
    source_tracks: list[Track],
) -> str:
    if playlist_kind is PlaylistKind.LIKED_TRACKS:
        return target.info.require_liked_tracks_target(target_cred)

    previous = await _previous_target_playlist_id(session, job=job, playlist_id=playlist_id)
    if previous and await _target_playlist_exists(target, target_cred, previous):
        return previous

    same_name_refs = [
        ref
        async for ref in target.iter_playlists(target_cred)
        if ref.name.strip() == playlist_name.strip()
    ]
    for ref in same_name_refs:
        try:
            target_playlist = await target.read_playlist(target_cred, ref)
        except NotFound:
            logger.warning(
                "skipping unreadable same-name target playlist playlist_id=%s", ref.id
            )
            continue
        if not target_playlist.tracks or has_track_overlap(source_tracks, target_playlist.tracks):
            return ref.id

    target_playlist_id = await target.create_playlist(
        target_cred,
        CreatePlaylistSpec(name=playlist_name, description=description, public=False),
    )
    session.add(
        orm.OperationLedger(
            job_id=job.id,
            op="create_playlist",
            intent={"source_playlist_id": playlist_id, "name": playlist_name},
            observed_target_id=target_playlist_id,
            state="done",
        )
    )
    return target_playlist_id


async def _previous_target_playlist_id(
    session: AsyncSession, *, job: orm.MigrationJob, playlist_id: str
) -> str | None:
    return await session.scalar(
        select(orm.JobItem.target_playlist_id)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.id != job.id,
            orm.MigrationJob.user_id == job.user_id,
            orm.MigrationJob.source_provider == job.source_provider,
            provider_account_history(
                orm.MigrationJob.source_account_id,
                current_account_id=job.source_account_id,
                user_id=job.user_id,
                provider=job.source_provider,
            ),
            orm.MigrationJob.target_provider == job.target_provider,
            provider_account_history(
                orm.MigrationJob.target_account_id,
                current_account_id=job.target_account_id,
                user_id=job.user_id,
                provider=job.target_provider,
            ),
            orm.JobItem.source_playlist_id == playlist_id,
            orm.JobItem.target_playlist_id.is_not(None),
        )
        .order_by(orm.JobItem.updated_at.desc())
        .limit(1)
    )


def _apply_review_history_bonus(
    prior_items: list[orm.JobItem],
    *,
    track: Track,
    result: MatchResult,
    review_threshold: float,
) -> MatchResult:
    prior = _prior_reviewed_item(track, prior_items, review_threshold)
    if prior is None or not prior.target_uri:
        return result
    confidence = min(
        1.0,
        round(
            max(result.confidence, prior.confidence or 0.0)
            + _REVIEW_HISTORY_CONFIDENCE_BONUS,
            4,
        ),
    )
    return MatchResult(
        candidate=_candidate_from_reviewed_item(prior),
        confidence=confidence,
        source="review_history",
        needs_review=confidence < review_threshold,
        review_reason=(
            "Previously accepted in another migration; confirm this match again."
            if confidence < review_threshold
            else None
        ),
    )


async def _previous_reviewed_items(
    session: AsyncSession,
    *,
    job: orm.MigrationJob,
    review_threshold: float,
) -> list[orm.JobItem]:
    stmt = (
        select(orm.JobItem)
        .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
        .where(
            orm.MigrationJob.id != job.id,
            orm.MigrationJob.user_id == job.user_id,
            orm.MigrationJob.source_provider == job.source_provider,
            provider_account_history(
                orm.MigrationJob.source_account_id,
                current_account_id=job.source_account_id,
                user_id=job.user_id,
                provider=job.source_provider,
            ),
            orm.MigrationJob.target_provider == job.target_provider,
            provider_account_history(
                orm.MigrationJob.target_account_id,
                current_account_id=job.target_account_id,
                user_id=job.user_id,
                provider=job.target_provider,
            ),
            orm.JobItem.status.in_(["written", "skipped"]),
            orm.JobItem.entity_type == MigrationEntityType.TRACK,
            orm.JobItem.target_uri.is_not(None),
            orm.JobItem.confidence.is_not(None),
            orm.JobItem.confidence < review_threshold,
        )
        .order_by(orm.JobItem.updated_at.desc())
    )
    return list((await session.execute(stmt)).scalars())


def _prior_reviewed_item(
    track: Track, prior_items: list[orm.JobItem], review_threshold: float
) -> orm.JobItem | None:
    keys = track_keys(track)
    if not keys:
        return None
    for item in prior_items:
        if item.entity_type not in (None, MigrationEntityType.TRACK.value):
            continue
        if not item.target_uri or item.confidence is None or item.confidence >= review_threshold:
            continue
        if _is_non_track_uri(item.target_uri):
            continue
        if item.status not in {"written", "skipped"}:
            continue
        if keys & _source_item_keys(item):
            return item
    return None


def _is_non_track_uri(uri: str) -> bool:
    lowered = uri.lower()
    return any(
        marker in lowered
        for marker in (":album:", ":artist:", "/album/", "/artist/")
    )


def _candidate_from_reviewed_item(item: orm.JobItem) -> TrackCandidate:
    target_uri = item.target_uri or ""
    return TrackCandidate(
        provider_track_id=_provider_track_id(target_uri),
        uri=target_uri,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        isrc=item.isrc,
    )


def _provider_track_id(uri: str) -> str:
    keys = [key.removeprefix("id:") for key in uri_keys(uri) if key.startswith("id:")]
    return keys[0] if keys else uri


def _source_item_keys(item: orm.JobItem) -> set[str]:
    return keys_from_metadata(
        item.source_metadata,
        title=item.title,
        artist=item.artist,
        album=item.album,
        duration_s=item.duration_s,
        isrc=item.isrc,
    )


async def _target_playlist_exists(
    target: ProviderAdapter, target_cred: ProviderCredential, playlist_id: str
) -> bool:
    try:
        await target.read_playlist(target_cred, PlaylistRef(id=playlist_id, name=playlist_id))
        return True
    except NotFound:
        return False


async def _target_playlist_keys(
    target: ProviderAdapter, target_cred: ProviderCredential, playlist_id: str
) -> set[str]:
    try:
        playlist = await target.read_playlist(
            target_cred, PlaylistRef(id=playlist_id, name=playlist_id)
        )
    except NotFound:
        logger.warning(
            "target playlist unavailable while checking duplicates playlist_id=%s",
            playlist_id,
        )
        return set()
    keys: set[str] = set()
    for track in playlist.tracks:
        keys.update(track_keys(track))
    return keys


async def _skip_duplicate_items(
    session: AsyncSession,
    job: orm.MigrationJob,
    items: list[orm.JobItem],
    *,
    existing_keys: set[str],
) -> list[orm.JobItem]:
    write_items: list[orm.JobItem] = []
    seen = set(existing_keys)
    for item in items:
        keys = _item_target_keys(item)
        if keys & seen:
            item.status = "skipped"
            item.reason = "duplicate already exists in target playlist"
            logger.info(
                "migration job_id=%s skipped duplicate item source_playlist_id=%s position=%s "
                "title=%r",
                job.id,
                item.source_playlist_id,
                item.position,
                item.title,
            )
            await commit_job_counts(session, job)
            continue
        seen.update(keys)
        write_items.append(item)
    return write_items


def _item_target_keys(item: orm.JobItem) -> set[str]:
    keys = uri_keys(item.target_uri)
    keys.update(
        keys_from_metadata(
            item.source_metadata,
            title=item.title,
            artist=item.artist,
            album=item.album,
            duration_s=item.duration_s,
            isrc=item.isrc,
        )
    )
    return keys


async def commit_job_counts(session: AsyncSession, job: orm.MigrationJob) -> None:
    await session.flush()
    total = await session.scalar(select(func.count()).where(orm.JobItem.job_id == job.id))
    done = await session.scalar(
        select(func.count()).where(
            orm.JobItem.job_id == job.id,
            orm.JobItem.status.in_(["written", "skipped", "needs_review"]),
        )
    )
    failed = await session.scalar(
        select(func.count()).where(orm.JobItem.job_id == job.id, orm.JobItem.status == "failed")
    )
    job.total = int(total or 0)
    job.done = int(done or 0)
    job.failed = int(failed or 0)
    await session.commit()


async def _playlist_status_counts(
    session: AsyncSession, job_id: str, playlist_id: str
) -> dict[str, int]:
    rows = await session.execute(
        select(orm.JobItem.status, func.count())
        .where(
            orm.JobItem.job_id == job_id,
            orm.JobItem.source_playlist_id == playlist_id,
        )
        .group_by(orm.JobItem.status)
    )
    return {status: int(count) for status, count in rows.all()}
