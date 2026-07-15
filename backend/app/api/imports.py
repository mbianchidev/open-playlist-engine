from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.api.dependencies import CurrentUserId
from app.db.base import get_session
from app.imports.models import LocalImportPreview
from app.imports.parsers import ImportLimitExceeded, PlaylistImportError
from app.imports.registry import parse_playlist_file, sanitize_filename
from app.imports.service import (
    LocalImportExpired,
    LocalImportNotFound,
    LocalImportStateError,
    cleanup_expired_imports,
    create_import,
    discard_import,
    load_preview_import,
    preview_from_record,
    spool_upload,
)
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/imports", tags=["local imports"])


class ImportErrorDetail(BaseModel):
    code: str
    message: str
    format: str | None = None


class ImportErrorResponse(BaseModel):
    detail: ImportErrorDetail


@router.post(
    "/preview",
    response_model=LocalImportPreview,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
    responses={
        400: {"model": ImportErrorResponse, "description": "Invalid upload request."},
        413: {"model": ImportErrorResponse, "description": "Upload-size limit exceeded."},
        422: {"model": ImportErrorResponse, "description": "Playlist parsing failed."},
    },
)
async def preview_import(
    request: Request,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    user_id: CurrentUserId,
) -> LocalImportPreview:
    limits = settings.local_import_limits
    _validate_content_length(request, limits.max_upload_bytes)
    await cleanup_expired_imports(session)
    await session.commit()
    spool = None
    try:
        spool, _ = await spool_upload(request.stream(), limits)
        result = await run_in_threadpool(
            parse_playlist_file,
            spool,
            filename=sanitize_filename(filename),
            limits=limits,
        )
        record = await create_import(
            session,
            user_id=user_id,
            filename=filename,
            result=result,
            settings=settings,
        )
        await session.commit()
        return preview_from_record(record)
    except ClientDisconnect as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "upload_interrupted", "message": "File upload was interrupted."},
        ) from exc
    except ImportLimitExceeded as exc:
        raise HTTPException(
            status_code=413 if exc.code == "upload_size_limit" else 422,
            detail=_import_error(exc),
        ) from exc
    except PlaylistImportError as exc:
        raise HTTPException(status_code=422, detail=_import_error(exc)) from exc
    finally:
        if spool is not None:
            spool.close()


@router.get("/{import_id}", response_model=LocalImportPreview)
async def get_import_preview(
    import_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> LocalImportPreview:
    try:
        record = await load_preview_import(session, import_id=import_id, user_id=user_id)
        await session.commit()
        return preview_from_record(record)
    except LocalImportNotFound as exc:
        raise HTTPException(status_code=404, detail="Local import not found") from exc
    except LocalImportExpired as exc:
        await session.commit()
        raise HTTPException(
            status_code=410,
            detail={
                "code": "import_expired",
                "message": "This local import expired. Upload the file again.",
            },
        ) from exc
    except LocalImportStateError as exc:
        raise HTTPException(status_code=409, detail=_state_error(exc)) from exc


@router.delete("/{import_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import(
    import_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> Response:
    try:
        await discard_import(session, import_id=import_id, user_id=user_id)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except LocalImportNotFound as exc:
        raise HTTPException(status_code=404, detail="Local import not found") from exc
    except LocalImportStateError as exc:
        raise HTTPException(status_code=409, detail=_state_error(exc)) from exc


def _validate_content_length(request: Request, max_bytes: int) -> None:
    raw_length = request.headers.get("content-length")
    if not raw_length:
        return
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_content_length", "message": "Invalid Content-Length header."},
        ) from exc
    if content_length > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "upload_size_limit",
                "message": f"Upload exceeds the configured {max_bytes}-byte limit.",
            },
        )


def _import_error(exc: PlaylistImportError) -> dict[str, str | None]:
    return {
        "code": exc.code,
        "message": str(exc),
        "format": exc.format.value if exc.format else None,
    }


def _state_error(exc: LocalImportStateError) -> dict[str, str]:
    return {"code": exc.code, "message": str(exc)}
