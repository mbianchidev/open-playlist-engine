"""The migration pipeline: import -> match -> review -> write.

Each phase is resumable. Writes go through the operation ledger
(:class:`app.db.models.OperationLedger`): we persist intent, call the provider,
persist the observed target id/position, and on uncertain failure reconcile by
*reading* target state instead of blindly retrying a non-idempotent call.
Progress is derived from ``job_item`` rows so a disconnected client can replay.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import CreatePlaylistSpec, ProviderError
from app.core.match_service import MatchService
from app.core.models import PlaylistRef, Track
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_sessionmaker
from app.db.repositories import load_fresh_credential
from app.settings import get_settings

logger = logging.getLogger(__name__)


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
        except Exception as exc:
            await session.rollback()
            job = await session.get(orm.MigrationJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(exc)
                await session.commit()
            logger.exception("migration job_id=%s failed", job_id)


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
    if not playlist_ids:
        raise ValueError("migration has no selected playlists")

    matcher = MatchService(graph=None, review_threshold=get_settings().review_confidence_threshold)
    for playlist_id in playlist_ids:
        playlist = await source.read_playlist(
            source_cred, PlaylistRef(id=playlist_id, name=playlist_id)
        )
        wanted = set((selection.get("tracks") or {}).get(playlist_id) or [])
        tracks = [
            track
            for track in playlist.tracks
            if not wanted or _track_selected(track, wanted)
        ]

        target_playlist_id = await target.create_playlist(
            target_cred,
            CreatePlaylistSpec(
                name=playlist.name,
                description=playlist.description
                or f"Migrated from {source.info.display_name} by Open Playlist Engine.",
                public=False,
            ),
        )
        session.add(
            orm.OperationLedger(
                job_id=job.id,
                op="create_playlist",
                intent={"source_playlist_id": playlist_id, "name": playlist.name},
                observed_target_id=target_playlist_id,
                state="done",
            )
        )
        await session.commit()

        item_pairs = await _create_items(session, job, playlist_id, playlist.name, tracks)
        matched: list[orm.JobItem] = []
        for item, track in item_pairs:
            item.target_playlist_id = target_playlist_id
            if not track.is_migratable:
                item.status = "skipped"
                item.reason = track.unsupported_reason or "unsupported playlist item"
                await _commit_counts(session, job)
                continue
            try:
                result = await matcher.resolve(track, target, target_cred)
            except ProviderError as exc:
                item.status = "failed"
                item.reason = str(exc)
                await _commit_counts(session, job)
                continue
            item.confidence = result.confidence
            if result.candidate is None:
                item.status = "failed"
                item.reason = "no target match found"
                await _commit_counts(session, job)
                continue
            item.target_uri = result.candidate.uri
            if result.needs_review:
                item.status = "needs_review"
                item.reason = f"match confidence {result.confidence:.2f} below review threshold"
                await _commit_counts(session, job)
                continue
            item.status = "matched"
            matched.append(item)
            await _commit_counts(session, job)

        if matched:
            try:
                results = await target.add_tracks(
                    target_cred, target_playlist_id, [item.target_uri or "" for item in matched]
                )
            except ProviderError as exc:
                for item in matched:
                    item.status = "failed"
                    item.reason = str(exc)
                await _commit_counts(session, job)
                continue
            for item, result in zip(matched, results, strict=False):
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
                else:
                    item.status = "failed"
                    item.reason = result.error or "target rejected track"
                await _commit_counts(session, job)
            for item in matched[len(results) :]:
                item.status = "failed"
                item.reason = "target did not return a result for this track"
                await _commit_counts(session, job)

    await _commit_counts(session, job)
    job.status = "done"
    await session.commit()
    logger.info("migration job_id=%s reached %s", job.id, Phase.DONE)


def _track_selected(track: Track, wanted: set[str]) -> bool:
    position = str(track.position) if track.position is not None else None
    identifiers = {
        value
        for value in [track.id, track.source_item_id, position]
        if value
    }
    return bool(identifiers & wanted)


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
            isrc=track.isrc,
            status="pending",
        )
        session.add(item)
        pairs.append((item, track))
    await _commit_counts(session, job)
    return pairs


async def _commit_counts(session: AsyncSession, job: orm.MigrationJob) -> None:
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
