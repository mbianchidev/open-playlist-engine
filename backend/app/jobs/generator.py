"""Snapshot a reviewed generation draft into the durable migration ledger."""

from __future__ import annotations

import uuid

from app.core.generator import GENERATED_SOURCE_PROVIDER, GenerationDraftNotConfirmable
from app.core.models import MigrationEntityType, Track
from app.db import models as orm


def build_confirmed_job(
    draft: orm.GenerationDraft,
    draft_items: list[orm.GenerationDraftItem],
) -> tuple[orm.MigrationJob, list[orm.JobItem]]:
    if draft.status != "draft":
        raise GenerationDraftNotConfirmable("Generation draft is already confirmed")
    if not draft_items:
        raise GenerationDraftNotConfirmable("Generation draft has no tracks")
    statuses = {item.status for item in draft_items}
    if "unresolved" in statuses or any(not item.target_uri for item in draft_items):
        raise GenerationDraftNotConfirmable(
            "Remove or replace unresolved tracks before confirmation"
        )
    if "needs_review" in statuses:
        raise GenerationDraftNotConfirmable(
            "Approve or replace every match that needs review before confirmation"
        )
    unexpected = statuses - {"resolved"}
    if unexpected:
        raise GenerationDraftNotConfirmable(
            f"Generation draft contains invalid item status: {sorted(unexpected)[0]}"
        )

    job_id = str(uuid.uuid4())
    job = orm.MigrationJob(
        id=job_id,
        user_id=draft.user_id,
        source_kind="generated",
        source_provider=GENERATED_SOURCE_PROVIDER,
        target_provider=draft.target_provider,
        source_account_id=draft.id,
        target_account_id=draft.target_account_id,
        selection={
            "playlist_ids": [draft.id],
            "tracks": {},
            "generated_playlist": {
                "draft_id": draft.id,
                "name": draft.name,
                "description": draft.description,
            },
        },
        status="pending",
        origin="generator",
    )
    job_items = []
    for item in sorted(draft_items, key=lambda value: value.position):
        track = Track(
            id=item.provider_track_id,
            title=item.resolved_title or item.intent_title,
            artist=item.resolved_artist or item.intent_artist,
            album=item.resolved_album or item.intent_album,
            duration_s=item.duration_s,
            explicit=item.explicit,
            isrc=item.isrc,
            provider_uris={draft.target_provider: item.target_uri or ""},
            metadata={
                "generator_intent": {
                    "title": item.intent_title,
                    "artist": item.intent_artist,
                    "album": item.intent_album,
                    "reason": item.intent_reason,
                },
                "provider_track_id": item.provider_track_id,
            },
            position=item.position,
            source_item_id=item.id,
        )
        job_items.append(
            orm.JobItem(
                id=str(uuid.uuid4()),
                job_id=job_id,
                entity_type=MigrationEntityType.TRACK.value,
                source_playlist_id=draft.id,
                source_playlist_name=draft.name,
                position=item.position,
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration_s=track.duration_s,
                explicit=track.explicit,
                isrc=track.isrc,
                source_metadata=track.model_dump(mode="json"),
                target_uri=item.target_uri,
                confidence=item.confidence,
                status="matched",
            )
        )
    draft.status = "confirmed"
    draft.confirmed_job_id = job.id
    return job, job_items
