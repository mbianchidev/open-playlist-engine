"""Migration jobs + live progress (phases 4-5).

Progress is delivered over SSE and derived from persisted ``job_item`` rows, so a
client that reconnects can resume via ``Last-Event-ID`` rather than losing state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/migrations", tags=["migrations"])


class Selection(BaseModel):
    playlist_ids: list[str] = []
    # optional per-playlist track filtering: {playlist_id: [track_ids]}
    tracks: dict[str, list[str]] = {}


class CreateMigration(BaseModel):
    source_provider: str
    target_provider: str
    source_account_id: str
    target_account_id: str
    selection: Selection


class JobView(BaseModel):
    id: str
    status: str
    total: int = 0
    done: int = 0
    failed: int = 0


@router.post("", response_model=JobView)
async def create_migration(body: CreateMigration) -> JobView:
    # TODO: validate capability matrix (source READ_TRACKS, target CREATE+ADD),
    # persist MigrationJob, enqueue arq run_migration.
    return JobView(id="todo-job-id", status="pending")


@router.get("/{job_id}", response_model=JobView)
async def get_migration(job_id: str) -> JobView:
    # TODO: load job + aggregate job_item counts.
    return JobView(id=job_id, status="pending")


async def _event_stream(job_id: str, request: Request) -> AsyncIterator[bytes]:
    last = 0
    while True:
        if await request.is_disconnected():
            break
        # TODO: read job_item rows with id > last; emit one SSE event each.
        payload = {"job_id": job_id, "cursor": last}
        yield f"id: {last}\nevent: progress\ndata: {json.dumps(payload)}\n\n".encode()
        last += 1
        await asyncio.sleep(2)


@router.get("/{job_id}/events")
async def migration_events(job_id: str, request: Request) -> StreamingResponse:
    return StreamingResponse(_event_stream(job_id, request), media_type="text/event-stream")
