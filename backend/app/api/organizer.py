from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.api.playlists import _playlist_refs
from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    NotFound,
    ProviderError,
    RateLimited,
)
from app.core.capabilities import Capability
from app.core.models import PlaylistKind, PlaylistRef
from app.core.organizer import (
    OrganizerAction,
    OrganizerIntent,
    OrganizerResolution,
    OrganizerSelection,
    UnsupportedOrganizerItem,
)
from app.core.organizer_service import (
    find_duplicate_candidates,
    resolve_organizer_selection,
)
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    load_fresh_credential,
)
from app.jobs.organizer import run_organizer
from app.jobs.queue import enqueue_or_inline

router = APIRouter(prefix="/api/organizer", tags=["organizer"])


class OrganizerRequest(BaseModel):
    provider: str
    account_id: str
    selection: OrganizerSelection = Field(default_factory=OrganizerSelection)
    confirmation: str | None = None


class OrganizerPlaylistView(BaseModel):
    playlist: PlaylistRef
    ownership: str
    available_intents: list[OrganizerIntent] = Field(default_factory=list)
    requires_ownership_check: bool = False
    notes: list[str] = Field(default_factory=list)


class OrganizerPreflightItemView(BaseModel):
    playlist_id: str
    playlist_name: str
    action: OrganizerAction
    destructive: bool
    ownership: str
    collaborative: bool | None = None
    selected_track_count: int = 0
    recovery: str


class OrganizerPreflightGroupView(BaseModel):
    action: OrganizerAction
    label: str
    destructive: bool
    recovery: str
    items: list[OrganizerPreflightItemView] = Field(default_factory=list)


class OrganizerPreflightView(BaseModel):
    code: str = "organizer_preflight"
    groups: list[OrganizerPreflightGroupView] = Field(default_factory=list)
    unsupported: list[UnsupportedOrganizerItem] = Field(default_factory=list)
    confirmation_required: bool = False
    confirmation_phrase: str | None = None
    total_playlists: int = 0
    total_tracks: int = 0


class DuplicateCandidateView(BaseModel):
    playlist_ids: tuple[str, str]
    playlist_names: tuple[str, str]
    normalized_name: str
    overlap_count: int
    overlap_ratio: float
    reasons: tuple[str, ...]


class OrganizerItemView(BaseModel):
    id: str
    playlist_id: str
    playlist_name: str
    action: str
    destructive: bool
    ownership: str
    collaborative: bool | None = None
    status: str
    attempts: int
    retryable: bool
    error: str | None = None
    request_payload: dict = Field(default_factory=dict)
    result_payload: dict = Field(default_factory=dict)


class OrganizerJobView(BaseModel):
    id: str
    provider: str
    account_id: str
    status: str
    total: int
    done: int
    failed: int
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    items: list[OrganizerItemView] = Field(default_factory=list)


@router.get("/playlists", response_model=list[OrganizerPlaylistView])
async def list_organizer_playlists(
    provider: str,
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    refresh: bool = False,
) -> list[OrganizerPlaylistView]:
    try:
        adapter = get(provider)
        credential, _ = await load_fresh_credential(
            session,
            account_id=account_id,
            adapter=adapter,
            provider=provider,
        )
        refs = await _playlist_refs(
            session,
            adapter=adapter,
            credential=credential,
            user_id=user_id,
            provider=provider,
            account_id=account_id,
            refresh=refresh,
        )
        await session.commit()
        return [_playlist_view(adapter, ref) for ref in refs]
    except (
        KeyError,
        AccountNotFound,
        CredentialNotFound,
        AuthExpired,
        RateLimited,
        AccessDenied,
        NotFound,
        ProviderError,
        ValueError,
    ) as exc:
        raise _http_error(exc) from exc


@router.post("/preflight", response_model=OrganizerPreflightView)
async def preflight_organizer(
    body: OrganizerRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> OrganizerPreflightView:
    del user_id
    try:
        resolution = await _resolve(session, body)
        await session.commit()
        return _preflight_view(resolution)
    except (
        KeyError,
        AccountNotFound,
        CredentialNotFound,
        AuthExpired,
        RateLimited,
        AccessDenied,
        NotFound,
        ProviderError,
        ValueError,
    ) as exc:
        raise _http_error(exc) from exc


@router.post("/duplicates", response_model=list[DuplicateCandidateView])
async def analyze_duplicates(
    body: OrganizerRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[DuplicateCandidateView]:
    del user_id
    try:
        adapter = get(body.provider)
        credential, _ = await load_fresh_credential(
            session,
            account_id=body.account_id,
            adapter=adapter,
            provider=body.provider,
        )
        candidates = await find_duplicate_candidates(adapter, credential)
        await session.commit()
        return [
            DuplicateCandidateView.model_validate(candidate.__dict__)
            for candidate in candidates
        ]
    except (
        KeyError,
        AccountNotFound,
        CredentialNotFound,
        AuthExpired,
        RateLimited,
        AccessDenied,
        NotFound,
        ProviderError,
        ValueError,
    ) as exc:
        raise _http_error(exc) from exc


@router.get("/jobs", response_model=list[OrganizerJobView])
async def list_organizer_jobs(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[OrganizerJobView]:
    jobs = list(
        (
            await session.execute(
                select(orm.OrganizerJob)
                .where(orm.OrganizerJob.user_id == user_id)
                .order_by(orm.OrganizerJob.created_at.desc(), orm.OrganizerJob.id.desc())
                .limit(20)
            )
        ).scalars()
    )
    items_by_job = await _items_by_job(session, [job.id for job in jobs])
    return [_job_view(job, items_by_job[job.id]) for job in jobs]


@router.post("/jobs", response_model=OrganizerJobView)
async def create_organizer_job(
    body: OrganizerRequest,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> OrganizerJobView:
    try:
        resolution = await _resolve(session, body)
    except (
        KeyError,
        AccountNotFound,
        CredentialNotFound,
        AuthExpired,
        RateLimited,
        AccessDenied,
        NotFound,
        ProviderError,
        ValueError,
    ) as exc:
        raise _http_error(exc) from exc
    _validate_job_request(resolution, body.confirmation)

    job = orm.OrganizerJob(
        user_id=user_id,
        provider=body.provider,
        account_id=body.account_id,
        request_payload=body.selection.model_dump(mode="json"),
        status="pending",
        total=len(resolution.items),
    )
    session.add(job)
    await session.flush()
    items: list[orm.OrganizerItem] = []
    for resolved in resolution.items:
        item = orm.OrganizerItem(
            job_id=job.id,
            playlist_id=resolved.playlist.id,
            playlist_name=resolved.playlist.name,
            action=resolved.action.value,
            destructive=resolved.destructive,
            ownership=_ownership(resolved.playlist),
            collaborative=resolved.playlist.collaborative,
            request_payload=resolved.request_payload,
            status="pending",
        )
        session.add(item)
        items.append(item)
    await session.commit()
    await enqueue_or_inline(
        background_tasks,
        function_name="run_organizer",
        fallback=run_organizer,
        job_id=job.id,
        job_label="organizer",
    )
    return _job_view(job, items)


@router.get("/jobs/{job_id}", response_model=OrganizerJobView)
async def get_organizer_job(
    job_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> OrganizerJobView:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="organizer job not found")
    items = await _job_items(session, job.id)
    return _job_view(job, items)


@router.post("/jobs/{job_id}/retry", response_model=OrganizerJobView)
async def retry_organizer_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> OrganizerJobView:
    job = await _owned_job(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="organizer job not found")
    items = await _job_items(session, job.id)
    retryable = [
        item for item in items if item.status == "failed" and item.retryable
    ]
    if not retryable:
        raise HTTPException(status_code=400, detail="organizer job has no retryable failures")
    for item in retryable:
        item.status = "pending"
        item.error = None
    job.status = "pending"
    job.error = None
    job.failed = 0
    await session.commit()
    await enqueue_or_inline(
        background_tasks,
        function_name="run_organizer",
        fallback=run_organizer,
        job_id=job.id,
        job_label="organizer retry",
    )
    return _job_view(job, items)


async def _resolve(session: AsyncSession, body: OrganizerRequest) -> OrganizerResolution:
    adapter = get(body.provider)
    credential, _ = await load_fresh_credential(
        session,
        account_id=body.account_id,
        adapter=adapter,
        provider=body.provider,
    )
    return await resolve_organizer_selection(adapter, credential, body.selection)


def _playlist_view(adapter, ref: PlaylistRef) -> OrganizerPlaylistView:
    intents: list[OrganizerIntent] = []
    requires_ownership_check = False
    notes: list[str] = []
    caps = adapter.info.capabilities
    if ref.kind is PlaylistKind.STANDARD:
        if caps.can(Capability.UNFOLLOW_PLAYLIST):
            intents.append(OrganizerIntent.REMOVE)
        if caps.can(Capability.DELETE_PLAYLIST) and ref.is_owned is not False:
            intents.append(OrganizerIntent.DELETE)
            requires_ownership_check = ref.is_owned is None
        if caps.can(Capability.REMOVE_TRACKS) and (
            ref.is_owned is True or ref.collaborative is True or ref.is_owned is None
        ):
            intents.append(OrganizerIntent.REMOVE_TRACKS)
            requires_ownership_check = requires_ownership_check or ref.is_owned is None
    if requires_ownership_check:
        notes.append("Ownership will be verified during preflight.")
    if not intents:
        notes.append(f"{adapter.info.display_name} exposes no safe organizer action here.")
    return OrganizerPlaylistView(
        playlist=ref,
        ownership=_ownership(ref),
        available_intents=intents,
        requires_ownership_check=requires_ownership_check,
        notes=notes,
    )


def _preflight_view(resolution: OrganizerResolution) -> OrganizerPreflightView:
    grouped: dict[OrganizerAction, list] = defaultdict(list)
    for item in resolution.items:
        grouped[item.action].append(item)
    groups = []
    for action, items in grouped.items():
        first = items[0]
        groups.append(
            OrganizerPreflightGroupView(
                action=action,
                label=_action_label(action),
                destructive=first.destructive,
                recovery=first.recovery,
                items=[
                    OrganizerPreflightItemView(
                        playlist_id=item.playlist.id,
                        playlist_name=item.playlist.name,
                        action=item.action,
                        destructive=item.destructive,
                        ownership=_ownership(item.playlist),
                        collaborative=item.playlist.collaborative,
                        selected_track_count=item.selected_track_count,
                        recovery=item.recovery,
                    )
                    for item in items
                ],
            )
        )
    return OrganizerPreflightView(
        groups=groups,
        unsupported=resolution.unsupported,
        confirmation_required=resolution.confirmation_phrase is not None,
        confirmation_phrase=resolution.confirmation_phrase,
        total_playlists=len(resolution.items) + len(resolution.unsupported),
        total_tracks=sum(item.selected_track_count for item in resolution.items),
    )


def _action_label(action: OrganizerAction) -> str:
    return {
        OrganizerAction.UNFOLLOW_PLAYLIST: "Remove from library",
        OrganizerAction.DELETE_PLAYLIST: "Delete permanently",
        OrganizerAction.REMOVE_TRACKS: "Remove selected songs",
    }[action]


def _ownership(ref: PlaylistRef) -> str:
    if ref.is_owned is True:
        return "owned"
    if ref.collaborative is True:
        return "collaborative"
    if ref.is_followed is True or ref.is_owned is False:
        return "followed"
    return "unknown"


def _item_view(item: orm.OrganizerItem) -> OrganizerItemView:
    return OrganizerItemView(
        id=item.id,
        playlist_id=item.playlist_id,
        playlist_name=item.playlist_name,
        action=item.action,
        destructive=item.destructive,
        ownership=item.ownership,
        collaborative=item.collaborative,
        status=item.status,
        attempts=item.attempts,
        retryable=item.retryable,
        error=item.error,
        request_payload=item.request_payload or {},
        result_payload=item.result_payload or {},
    )


def _job_view(
    job: orm.OrganizerJob,
    items: list[orm.OrganizerItem],
) -> OrganizerJobView:
    return OrganizerJobView(
        id=job.id,
        provider=job.provider,
        account_id=job.account_id,
        status=job.status,
        total=job.total,
        done=job.done,
        failed=job.failed,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        items=[_item_view(item) for item in items],
    )


async def _job_items(
    session: AsyncSession,
    job_id: str,
) -> list[orm.OrganizerItem]:
    return list(
        (
            await session.execute(
                select(orm.OrganizerItem)
                .where(orm.OrganizerItem.job_id == job_id)
                .order_by(orm.OrganizerItem.created_at, orm.OrganizerItem.id)
            )
        ).scalars()
    )


async def _items_by_job(
    session: AsyncSession,
    job_ids: list[str],
) -> dict[str, list[orm.OrganizerItem]]:
    grouped = {job_id: [] for job_id in job_ids}
    if not job_ids:
        return grouped
    items = list(
        (
            await session.execute(
                select(orm.OrganizerItem)
                .where(orm.OrganizerItem.job_id.in_(job_ids))
                .order_by(orm.OrganizerItem.created_at, orm.OrganizerItem.id)
            )
        ).scalars()
    )
    for item in items:
        grouped[item.job_id].append(item)
    return grouped


async def _owned_job(
    session: AsyncSession,
    *,
    job_id: str,
    user_id: str,
) -> orm.OrganizerJob | None:
    return await session.scalar(_owned_job_stmt(job_id, user_id))


def _owned_job_stmt(job_id: str, user_id: str):
    return select(orm.OrganizerJob).where(
        orm.OrganizerJob.id == job_id,
        orm.OrganizerJob.user_id == user_id,
    )


def _validate_job_request(
    resolution: OrganizerResolution,
    confirmation: str | None,
) -> None:
    preflight = _preflight_view(resolution)
    if resolution.unsupported:
        raise HTTPException(status_code=409, detail=preflight.model_dump(mode="json"))
    if not resolution.items:
        raise HTTPException(status_code=400, detail="No supported organizer actions were selected")
    if resolution.confirmation_phrase and (
        (confirmation or "").strip() != resolution.confirmation_phrase
    ):
        detail = preflight.model_copy(update={"code": "organizer_confirmation_required"})
        raise HTTPException(status_code=409, detail=detail.model_dump(mode="json"))


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AccountNotFound, CredentialNotFound, NotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (ProviderError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Organizer request failed")
