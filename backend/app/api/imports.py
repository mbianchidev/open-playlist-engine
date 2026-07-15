from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.core.adapter import AccessDenied, AuthExpired, NotFound, ProviderError, RateLimited
from app.core.models import Playlist
from app.db import models as orm
from app.db.base import get_session
from app.imports.http import SafeHttpError
from app.imports.models import ImportIssue
from app.imports.parser import ImportLimitExceeded
from app.imports.service import (
    ImportContentError,
    ImportService,
    SourceConnectionRequired,
)
from app.imports.urls import UnsafePlaylistUrl

router = APIRouter(prefix="/api/imports", tags=["imports"])


class UrlImportPreviewRequest(BaseModel):
    kind: Literal["url"]
    url: str
    source_account_id: str | None = None


class TextImportPreviewRequest(BaseModel):
    kind: Literal["text"]
    text: str
    name: str | None = None


ImportPreviewRequest = Annotated[
    UrlImportPreviewRequest | TextImportPreviewRequest,
    Field(discriminator="kind"),
]


class ImportSourceView(BaseModel):
    provider: str
    label: str
    locator: str


class ImportPreviewView(BaseModel):
    import_id: str
    source: ImportSourceView
    playlist: Playlist
    issues: list[ImportIssue] = Field(default_factory=list)
    track_count: int
    unsupported_count: int


def get_import_service() -> ImportService:
    return ImportService()


@router.post("/preview", response_model=ImportPreviewView)
async def preview_import(
    body: ImportPreviewRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    service: Annotated[ImportService, Depends(get_import_service)],
) -> ImportPreviewView:
    try:
        if isinstance(body, TextImportPreviewRequest):
            row = await service.preview_text(
                session,
                user_id=user_id,
                text=body.text,
                name=body.name,
            )
        else:
            row = await service.preview_url(
                session,
                user_id=user_id,
                url=body.url,
                source_account_id=body.source_account_id,
            )
        await session.commit()
        return _preview_view(row)
    except SourceConnectionRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_connection_required",
                "message": str(exc),
                "provider": exc.provider,
                "action": "connect_source",
            },
        ) from exc
    except ImportLimitExceeded as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (UnsafePlaylistUrl, ImportContentError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SafeHttpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (KeyError, ProviderError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _preview_view(row: orm.ImportedPlaylist) -> ImportPreviewView:
    playlist = Playlist.model_validate(row.playlist)
    issues = [ImportIssue.model_validate(issue) for issue in row.issues or []]
    return ImportPreviewView(
        import_id=row.id,
        source=ImportSourceView(
            provider=row.source_provider,
            label=row.source_label,
            locator=row.source_locator,
        ),
        playlist=playlist,
        issues=issues,
        track_count=len(playlist.tracks),
        unsupported_count=sum(not track.is_migratable for track in playlist.tracks),
    )
