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
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import (
    CreatePlaylistSpec,
    NotFound,
    ProviderAdapter,
    ProviderCredential,
    ProviderError,
    TrackCandidate,
)
from app.core.match_service import MatchResult, MatchService
from app.core.migration_state import (
    has_track_overlap,
    keys_from_metadata,
    track_keys,
    track_selected,
    uri_keys,
)
from app.core.models import PlaylistKind, PlaylistRef, Track
from app.core.registry import get
from app.db import models as orm
from app.db.account_scope import provider_account_history
from app.db.base import get_sessionmaker
from app.db.repositories import load_fresh_credential
from app.imports.migration import load_import_source
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
    target = get(job.target_provider)
    target_cred, _ = await load_fresh_credential(
        session,
        account_id=job.target_account_id,
        adapter=target,
        provider=job.target_provider,
        user_id=job.user_id,
    )
    selection = job.selection or {}
    source_import_id = selection.get("source_import_id")
    if isinstance(source_import_id, str) and source_import_id:
        imported_source = await load_import_source(
            session,
            import_id=source_import_id,
            user_id=job.user_id,
        )
        source = None
        source_cred = None
        source_display_name = imported_source.label
    else:
        imported_source = None
        source = get(job.source_provider)
        source_cred, _ = await load_fresh_credential(
            session,
            account_id=job.source_account_id,
            adapter=source,
            provider=job.source_provider,
            user_id=job.user_id,
        )
        source_display_name = source.info.display_name
    job.status = "running"
    job.error = None
    await session.commit()

    playlist_ids = list(selection.get("playlist_ids") or [])
    if not playlist_ids:
        raise ValueError("migration has no selected playlists")
    logger.info(
        "migration job_id=%s running source=%s target=%s playlist_count=%s",
        job.id,
        job.source_provider,
        job.target_provider,
        len(playlist_ids),
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
        if imported_source is not None:
            if imported_source.playlist.id != playlist_id:
                raise ValueError("migration import snapshot does not match selected playlist")
            playlist = imported_source.playlist
        else:
            if source is None or source_cred is None:
                raise ValueError("migration source is unavailable")
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
            or f"Migrated from {source_display_name} by Open Playlist Engine.",
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
        if not item.target_uri or item.confidence is None or item.confidence >= review_threshold:
            continue
        if item.status not in {"written", "skipped"}:
            continue
        if keys & _source_item_keys(item):
            return item
    return None


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
