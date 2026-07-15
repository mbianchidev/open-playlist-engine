from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    NotFound,
    ProviderError,
    RateLimited,
)
from app.core.models import PlaylistRef, PlaylistSelection
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    load_fresh_credential,
)
from app.exports.history import HistoryPlaylistLoader
from app.exports.models import ExportFormat
from app.exports.service import (
    ExportArtifact,
    ExportGenerationError,
    LoadedPlaylist,
    build_export_artifact,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/exports", tags=["exports"])

_BINARY_RESPONSE_CONTENT = {
    "text/csv": {"schema": {"type": "string", "format": "binary"}},
    "text/plain": {"schema": {"type": "string", "format": "binary"}},
    "application/vnd.apple.mpegurl": {
        "schema": {"type": "string", "format": "binary"}
    },
    "application/xspf+xml": {"schema": {"type": "string", "format": "binary"}},
    "application/vnd.open-playlist+json": {
        "schema": {"type": "string", "format": "binary"}
    },
    "application/zip": {"schema": {"type": "string", "format": "binary"}},
}
_DOWNLOAD_RESPONSES = {
    200: {
        "description": "A portable playlist file or multi-playlist ZIP archive.",
        "headers": {
            "Content-Disposition": {
                "description": "Deterministic sanitized download filename.",
                "schema": {"type": "string"},
            },
            "X-Open-Playlist-Warning-Count": {
                "description": "Number of warnings represented in the output.",
                "schema": {"type": "integer"},
            },
        },
        "content": _BINARY_RESPONSE_CONTENT,
    }
}


class CreateExport(BaseModel):
    source_provider: str
    source_account_id: str
    format: ExportFormat
    selection: PlaylistSelection


class CreateHistoryExport(BaseModel):
    format: ExportFormat


class _LivePlaylistLoader:
    def __init__(self, adapter, credential) -> None:
        self._adapter = adapter
        self._credential = credential

    async def load(self, playlist_id: str) -> LoadedPlaylist:
        playlist = await self._adapter.read_playlist(
            self._credential,
            PlaylistRef(id=playlist_id, name=playlist_id),
        )
        return LoadedPlaylist(playlist=playlist)


@router.post("", response_class=StreamingResponse, responses=_DOWNLOAD_RESPONSES)
async def create_export(
    body: CreateExport,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> StreamingResponse:
    try:
        artifact = await _build_live_export(body, session, user_id=user_id)
        return _download_response(artifact)
    except HTTPException:
        raise
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
    except ExportGenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("could not persist refreshed export credentials")
        raise HTTPException(
            status_code=500,
            detail="Could not save refreshed source credentials",
        ) from exc
    except OSError as exc:
        logger.exception("local playlist export failed")
        raise HTTPException(
            status_code=500,
            detail="Could not create the local export file",
        ) from exc


@router.post(
    "/migrations/{job_id}",
    response_class=StreamingResponse,
    responses=_DOWNLOAD_RESPONSES,
)
async def create_history_export(
    job_id: str,
    body: CreateHistoryExport,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> StreamingResponse:
    try:
        artifact = await _build_history_export(
            job_id,
            body,
            session,
            user_id=user_id,
        )
        return _download_response(artifact)
    except HTTPException:
        raise
    except ExportGenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("historical playlist export failed job_id=%s", job_id)
        raise HTTPException(
            status_code=500,
            detail="Could not create the historical export file",
        ) from exc


async def _build_live_export(
    body: CreateExport,
    session: AsyncSession,
    *,
    user_id: str,
) -> ExportArtifact:
    adapter = get(body.source_provider)
    credential, account = await load_fresh_credential(
        session,
        account_id=body.source_account_id,
        adapter=adapter,
        provider=body.source_provider,
    )
    if account.user_id != user_id:
        raise AccountNotFound(body.source_account_id)
    await session.commit()
    return await build_export_artifact(
        export_format=body.format,
        source_provider=body.source_provider,
        selection=body.selection,
        loader=_LivePlaylistLoader(adapter, credential),
        max_playlists=get_settings().export_max_playlists,
    )


async def _build_history_export(
    job_id: str,
    body: CreateHistoryExport,
    session: AsyncSession,
    *,
    user_id: str,
) -> ExportArtifact:
    statement = select(orm.MigrationJob).where(
        orm.MigrationJob.id == job_id,
        orm.MigrationJob.user_id == user_id,
    )
    job = (await session.execute(statement)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    if job.status not in {"done", "failed"}:
        raise HTTPException(
            status_code=409,
            detail="Only completed or failed migrations can be exported",
        )
    selection = PlaylistSelection.model_validate(job.selection or {})
    return await build_export_artifact(
        export_format=body.format,
        source_provider=job.source_provider,
        selection=selection,
        loader=HistoryPlaylistLoader(session, job),
        max_playlists=get_settings().export_max_playlists,
    )


def _download_response(artifact: ExportArtifact) -> StreamingResponse:
    try:
        size = artifact.path.stat().st_size
    except OSError:
        artifact.cleanup()
        raise
    filename = quote(artifact.filename, safe="")
    return StreamingResponse(
        artifact.stream(),
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Open-Playlist-Schema-Version": "1",
            "X-Open-Playlist-Warning-Count": str(len(artifact.warnings)),
        },
    )
