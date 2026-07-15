"""Owner management and public read/download routes for playlist shares."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    AccountView,
    ConnectionView,
    account_view,
    begin_connection,
    complete_connection,
)
from app.api.dependencies import CurrentUserId
from app.api.migrations import (
    BatchReview,
    JobItemView,
    JobView,
    MigrationWarningsView,
    ReviewItem,
    _apply_review,
    _enqueue_or_inline,
    _event_stream,
    _item_view,
    _job_view,
    _job_wait_remaining,
    _same_name_warnings,
    _tracks_migrated_today,
    _validate_target_capabilities,
    _warning,
)
from app.core.adapter import (
    AccessDenied,
    AuthChallenge,
    AuthExpired,
    ChallengeShape,
    NotFound,
    ProviderError,
    RateLimited,
)
from app.core.models import PlaylistRef
from app.core.rate_limit import rate_limiter
from app.core.registry import get
from app.core.share_sessions import (
    ensure_recipient_session,
    recipient_user_id,
    require_recipient_session,
)
from app.core.sharing import (
    PortableFormat,
    SharedPlaylistSnapshot,
    ShareVisibility,
    SnapshotLimitError,
    build_shared_snapshot,
    hash_share_token,
    render_share_html,
    serialize_snapshot,
    snapshot_to_playlist,
)
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    delete_expired_ephemeral_accounts,
    list_accounts,
    load_fresh_credential,
)
from app.db.shares import (
    ShareNotFound,
    ShareUnavailable,
    create_playlist_share,
    decrypt_share_token,
    load_public_share,
    revoke_playlist_share,
    save_recipient_auth_state,
    share_unavailable_reason,
)
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/shares", tags=["shares"])
public_router = APIRouter(prefix="/api/public/shares", tags=["public shares"])
page_router = APIRouter(tags=["public shares"])


class ShareConfigView(BaseModel):
    enabled: bool
    disabled_reason: str
    public_base_url: str | None = None
    max_tracks: int
    max_expiry_days: int
    supported_download_formats: list[PortableFormat]


class CreateShare(BaseModel):
    provider: str
    account_id: str
    playlist_id: str
    attribution: str | None = None
    visibility: ShareVisibility = ShareVisibility.UNLISTED
    expires_at: datetime | None = None


class UpdateShare(BaseModel):
    visibility: ShareVisibility | None = None
    expires_at: datetime | None = None


class ShareDetailView(BaseModel):
    id: str
    url: str
    status: str
    visibility: ShareVisibility
    snapshot: SharedPlaylistSnapshot
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PublicShareView(BaseModel):
    visibility: ShareVisibility
    snapshot: SharedPlaylistSnapshot
    expires_at: datetime | None = None
    download_formats: list[PortableFormat]


class RecipientImport(BaseModel):
    target_provider: str
    target_account_id: str
    acknowledge_warnings: bool = False


def _require_sharing(settings: Settings) -> None:
    if not settings.sharing_enabled:
        raise HTTPException(status_code=503, detail=settings.sharing_disabled_reason)


def _status(share: orm.PlaylistShare) -> str:
    return share_unavailable_reason(share) or "active"


def _snapshot(
    share: orm.PlaylistShare,
    *,
    max_bytes: int | None = None,
) -> SharedPlaylistSnapshot:
    snapshot = SharedPlaylistSnapshot.model_validate(share.snapshot)
    if max_bytes is not None and len(snapshot.model_dump_json().encode()) > max_bytes:
        raise HTTPException(status_code=413, detail="playlist share snapshot is too large")
    return snapshot


def _share_url(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url_normalized}/share/{token}"


def _share_view(share: orm.PlaylistShare, settings: Settings) -> ShareDetailView:
    return ShareDetailView(
        id=share.id,
        url=_share_url(settings, decrypt_share_token(share)),
        status=_status(share),
        visibility=ShareVisibility(share.visibility),
        snapshot=_snapshot(share),
        expires_at=share.expires_at,
        revoked_at=share.revoked_at,
        created_at=share.created_at,
        updated_at=share.updated_at,
    )


async def _owned_share(
    session: AsyncSession, *, share_id: str, user_id: str
) -> orm.PlaylistShare:
    share = await session.scalar(
        select(orm.PlaylistShare).where(
            orm.PlaylistShare.id == share_id,
            orm.PlaylistShare.owner_user_id == user_id,
        )
    )
    if share is None:
        raise HTTPException(status_code=404, detail="playlist share not found")
    return share


def _validated_expiry(
    value: datetime | None,
    *,
    settings: Settings,
    require_future: bool,
) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(status_code=400, detail="expires_at must include a timezone")
    now = datetime.now(UTC)
    normalized = value.astimezone(UTC)
    if require_future and normalized <= now:
        raise HTTPException(status_code=400, detail="expires_at must be in the future")
    if normalized > now + timedelta(days=settings.share_max_expiry_days):
        raise HTTPException(
            status_code=400,
            detail=f"expires_at cannot exceed {settings.share_max_expiry_days} days",
        )
    return normalized


def _provider_http_error(exc: ProviderError) -> HTTPException:
    if isinstance(exc, AuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/config", response_model=ShareConfigView)
async def share_config(
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShareConfigView:
    del user_id
    return ShareConfigView(
        enabled=settings.sharing_enabled,
        disabled_reason=settings.sharing_disabled_reason,
        public_base_url=(
            settings.public_base_url_normalized if settings.sharing_enabled else None
        ),
        max_tracks=settings.share_max_tracks,
        max_expiry_days=settings.share_max_expiry_days,
        supported_download_formats=list(PortableFormat),
    )


@router.get("", response_model=list[ShareDetailView])
async def list_shares(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ShareDetailView]:
    _require_sharing(settings)
    shares = list(
        (
            await session.execute(
                select(orm.PlaylistShare)
                .where(orm.PlaylistShare.owner_user_id == user_id)
                .order_by(orm.PlaylistShare.created_at.desc(), orm.PlaylistShare.id.desc())
            )
        ).scalars()
    )
    return [_share_view(share, settings) for share in shares]


@router.post("", response_model=ShareDetailView, status_code=201)
async def create_share(
    body: CreateShare,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShareDetailView:
    _require_sharing(settings)
    expires_at = _validated_expiry(
        body.expires_at,
        settings=settings,
        require_future=True,
    )
    try:
        adapter = get(body.provider)
        credential, _ = await load_fresh_credential(
            session,
            account_id=body.account_id,
            adapter=adapter,
            provider=body.provider,
            user_id=user_id,
        )
        playlist = await adapter.read_playlist(
            credential,
            PlaylistRef(id=body.playlist_id, name=body.playlist_id),
        )
        snapshot = build_shared_snapshot(
            playlist,
            provider=body.provider,
            playlist_id=body.playlist_id,
            attribution=body.attribution,
            approved_artwork_hosts=settings.approved_share_artwork_hosts,
            max_tracks=settings.share_max_tracks,
            max_bytes=settings.share_max_snapshot_bytes,
        )
        share, _ = await create_playlist_share(
            session,
            owner_user_id=user_id,
            snapshot=snapshot,
            visibility=body.visibility,
            expires_at=expires_at,
        )
        await session.commit()
        await session.refresh(share)
        return _share_view(share, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SnapshotLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc


@router.get("/{share_id}", response_model=ShareDetailView)
async def get_share(
    share_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShareDetailView:
    _require_sharing(settings)
    share = await _owned_share(session, share_id=share_id, user_id=user_id)
    return _share_view(share, settings)


@router.patch("/{share_id}", response_model=ShareDetailView)
async def update_share(
    share_id: str,
    body: UpdateShare,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShareDetailView:
    _require_sharing(settings)
    share = await _owned_share(session, share_id=share_id, user_id=user_id)
    if body.visibility is not None:
        share.visibility = body.visibility.value
    if "expires_at" in body.model_fields_set:
        share.expires_at = _validated_expiry(
            body.expires_at,
            settings=settings,
            require_future=False,
        )
    await session.commit()
    await session.refresh(share)
    return _share_view(share, settings)


@router.post("/{share_id}/expire", response_model=ShareDetailView)
async def expire_share(
    share_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShareDetailView:
    _require_sharing(settings)
    share = await _owned_share(session, share_id=share_id, user_id=user_id)
    share.expires_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(share)
    return _share_view(share, settings)


@router.post("/{share_id}/revoke", response_model=ShareDetailView)
async def revoke_share(
    share_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShareDetailView:
    _require_sharing(settings)
    share = await _owned_share(session, share_id=share_id, user_id=user_id)
    await revoke_playlist_share(session, share)
    await session.commit()
    await session.refresh(share)
    return _share_view(share, settings)


async def _public_rate_limit(
    token: str,
    action: str,
    settings: Settings,
    *,
    cost: float = 1.0,
) -> None:
    retry_after = await rate_limiter.try_consume(
        f"public-share:{hash_share_token(token)}:{action}",
        capacity=settings.share_rate_limit_capacity,
        refill_per_s=settings.share_rate_limit_refill_per_s,
        cost=cost,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Public playlist share rate limit exceeded",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )


async def _load_public_or_http(
    session: AsyncSession, token: str, *, require_active: bool = True
) -> orm.PlaylistShare:
    try:
        return await load_public_share(
            session,
            token,
            require_active=require_active,
        )
    except ShareNotFound as exc:
        raise HTTPException(status_code=404, detail="playlist share not found") from exc
    except ShareUnavailable as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc


def _public_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


@public_router.get("/{token}", response_model=PublicShareView)
async def public_share(
    token: str,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PublicShareView:
    _require_sharing(settings)
    await _public_rate_limit(token, "view", settings)
    share = await _load_public_or_http(session, token)
    for name, value in _public_headers().items():
        response.headers[name] = value
    return PublicShareView(
        visibility=ShareVisibility(share.visibility),
        snapshot=_snapshot(share, max_bytes=settings.share_max_snapshot_bytes),
        expires_at=share.expires_at,
        download_formats=list(PortableFormat),
    )


@public_router.get("/{token}/accounts", response_model=list[AccountView])
async def recipient_accounts(
    token: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[AccountView]:
    _require_sharing(settings)
    await _public_rate_limit(token, "accounts", settings)
    share = await _load_public_or_http(session, token)
    session_id = ensure_recipient_session(request, response, settings)
    user_id = recipient_user_id(share.id, session_id)
    removed = await delete_expired_ephemeral_accounts(session)
    rows = await list_accounts(session, user_id=user_id)
    if removed:
        await session.commit()
    for name, value in _public_headers().items():
        response.headers[name] = value
    return [account_view(account) for account in rows]


@public_router.post("/{token}/auth/{provider}/begin", response_model=AuthChallenge)
async def begin_recipient_auth(
    token: str,
    provider: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthChallenge:
    _require_sharing(settings)
    await _public_rate_limit(token, f"auth-begin:{provider}", settings)
    share = await _load_public_or_http(session, token)
    if not settings.recipient_redirect_ready(provider):
        raise HTTPException(
            status_code=503,
            detail=(
                f"{provider} recipient OAuth requires its redirect URI to use "
                "OPE_PUBLIC_BASE_URL"
            ),
        )
    session_id = ensure_recipient_session(request, response, settings)
    user_id = recipient_user_id(share.id, session_id)
    challenge = await begin_connection(provider, user_id=user_id)
    if challenge.shape is ChallengeShape.REDIRECT:
        if not challenge.state:
            raise HTTPException(status_code=502, detail="Provider redirect is missing OAuth state")
        await save_recipient_auth_state(
            session,
            state=challenge.state,
            share_id=share.id,
            recipient_user_id=user_id,
            provider=provider,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        await session.commit()
    for name, value in _public_headers().items():
        response.headers[name] = value
    return challenge


@public_router.post("/{token}/auth/{provider}/complete", response_model=ConnectionView)
async def complete_recipient_auth(
    token: str,
    provider: str,
    callback: dict,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConnectionView:
    _require_sharing(settings)
    await _public_rate_limit(token, f"auth-complete:{provider}", settings)
    share = await _load_public_or_http(session, token)
    user_id = recipient_user_id(
        share.id,
        require_recipient_session(request, settings),
    )
    return await complete_connection(
        provider,
        callback,
        session,
        user_id=user_id,
        ephemeral_expires_at=datetime.now(UTC)
        + timedelta(seconds=settings.share_recipient_credential_retention_s),
    )


@public_router.post("/{token}/imports", response_model=JobView)
async def import_shared_playlist(
    token: str,
    body: RecipientImport,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobView:
    _require_sharing(settings)
    await _public_rate_limit(token, "import", settings)
    share = await _load_public_or_http(session, token)
    user_id = recipient_user_id(
        share.id,
        require_recipient_session(request, settings),
    )
    try:
        target = get(body.target_provider)
        target_credential, _ = await load_fresh_credential(
            session,
            account_id=body.target_account_id,
            adapter=target,
            provider=body.target_provider,
            user_id=user_id,
        )
        warnings = await _public_import_warnings(
            session,
            share=share,
            target=target,
            target_credential=target_credential,
            target_provider=body.target_provider,
            target_account_id=body.target_account_id,
            user_id=user_id,
            settings=settings,
        )
        if warnings and not body.acknowledge_warnings:
            await session.commit()
            raise HTTPException(
                status_code=409,
                detail=MigrationWarningsView(warnings=warnings).model_dump(),
            )
        snapshot = _snapshot(share, max_bytes=settings.share_max_snapshot_bytes)
        job = orm.MigrationJob(
            user_id=user_id,
            source_provider="share",
            source_account_id=share.id,
            source_share_id=share.id,
            source_snapshot=snapshot.model_dump(mode="json"),
            target_provider=body.target_provider,
            target_account_id=body.target_account_id,
            selection={"playlist_ids": [share.id], "tracks": {}},
            status="pending",
            total=len(snapshot.tracks),
        )
        session.add(job)
        await session.commit()
        await _enqueue_or_inline(background_tasks, job.id)
        return _job_view(job)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc


async def _public_import_warnings(
    session: AsyncSession,
    *,
    share: orm.PlaylistShare,
    target,
    target_credential,
    target_provider: str,
    target_account_id: str,
    user_id: str,
    settings: Settings,
) -> list[dict[str, str]]:
    snapshot = _snapshot(share, max_bytes=settings.share_max_snapshot_bytes)
    playlist = snapshot_to_playlist(snapshot)
    total_tracks = len(playlist.tracks)
    active_jobs = await session.scalar(
        select(func.count())
        .select_from(orm.MigrationJob)
        .where(
            orm.MigrationJob.source_share_id == share.id,
            orm.MigrationJob.status.in_(["pending", "running"]),
        )
    )
    if int(active_jobs or 0) >= settings.share_import_max_concurrent_jobs:
        raise HTTPException(
            status_code=429,
            detail="This playlist share has too many active imports",
        )
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    imported_today = await session.scalar(
        select(func.sum(orm.MigrationJob.total)).where(
            orm.MigrationJob.source_share_id == share.id,
            orm.MigrationJob.created_at >= today,
        )
    )
    if int(imported_today or 0) + total_tracks > settings.share_import_daily_track_limit:
        raise HTTPException(
            status_code=429,
            detail="This playlist share reached its daily public import limit",
        )

    selected = {share.id: playlist}
    _validate_target_capabilities(target, target_credential, selected)
    warnings: list[dict[str, str]] = []
    if total_tracks > settings.migration_safe_max_tracks_per_job:
        warnings.append(
            _warning(
                "track_count",
                f"Safe default is {settings.migration_safe_max_tracks_per_job} tracks "
                f"per job; this shared playlist has {total_tracks}.",
            )
        )
    migrated_today = await _tracks_migrated_today(
        session,
        user_id=user_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
    )
    if migrated_today + total_tracks > settings.migration_safe_daily_tracks:
        warnings.append(
            _warning(
                "daily_limit",
                f"Safe default is {settings.migration_safe_daily_tracks} tracks/day; "
                f"today would reach {migrated_today + total_tracks}.",
            )
        )
    wait_remaining = await _job_wait_remaining(
        session,
        user_id=user_id,
        target_provider=target_provider,
        target_account_id=target_account_id,
        min_gap_s=settings.migration_safe_min_job_gap_s,
    )
    if wait_remaining > 0:
        warnings.append(
            _warning(
                "job_spacing",
                f"Wait about {wait_remaining} seconds before another import.",
            )
        )
    warnings.extend(await _same_name_warnings(target, target_credential, selected))
    return warnings


async def _recipient_job(
    session: AsyncSession,
    *,
    share_id: str,
    user_id: str,
    job_id: str,
) -> orm.MigrationJob:
    job = await session.scalar(
        select(orm.MigrationJob).where(
            orm.MigrationJob.id == job_id,
            orm.MigrationJob.user_id == user_id,
            orm.MigrationJob.source_share_id == share_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="migration job not found")
    return job


async def _recipient_job_context(
    token: str,
    job_id: str,
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> tuple[orm.PlaylistShare, str, orm.MigrationJob]:
    share = await _load_public_or_http(session, token, require_active=False)
    user_id = recipient_user_id(
        share.id,
        require_recipient_session(request, settings),
    )
    job = await _recipient_job(
        session,
        share_id=share.id,
        user_id=user_id,
        job_id=job_id,
    )
    return share, user_id, job


@public_router.get("/{token}/imports/{job_id}", response_model=JobView)
async def get_recipient_import(
    token: str,
    job_id: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobView:
    _require_sharing(settings)
    await _public_rate_limit(token, "import-progress", settings)
    _, _, job = await _recipient_job_context(
        token,
        job_id,
        request,
        session,
        settings,
    )
    for name, value in _public_headers().items():
        response.headers[name] = value
    return _job_view(job)


@public_router.get("/{token}/imports/{job_id}/items", response_model=list[JobItemView])
async def get_recipient_import_items(
    token: str,
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[JobItemView]:
    _require_sharing(settings)
    await _public_rate_limit(token, "import-items", settings)
    await _recipient_job_context(token, job_id, request, session, settings)
    rows = (
        await session.execute(
            select(orm.JobItem)
            .where(orm.JobItem.job_id == job_id)
            .order_by(orm.JobItem.source_playlist_id, orm.JobItem.position)
        )
    ).scalars()
    return [_item_view(item) for item in rows]


@public_router.post(
    "/{token}/imports/{job_id}/items/{item_id}/review",
    response_model=JobItemView,
)
async def review_recipient_import_item(
    token: str,
    job_id: str,
    item_id: str,
    body: ReviewItem,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobItemView:
    _require_sharing(settings)
    await _public_rate_limit(token, "import-review", settings)
    _, _, job = await _recipient_job_context(
        token,
        job_id,
        request,
        session,
        settings,
    )
    item = await session.get(orm.JobItem, item_id)
    if item is None or item.job_id != job_id:
        raise HTTPException(status_code=404, detail="migration item not found")
    return await _apply_review(session, job, item, body)


@public_router.post(
    "/{token}/imports/{job_id}/items/review",
    response_model=list[JobItemView],
)
async def review_recipient_import_items(
    token: str,
    job_id: str,
    body: BatchReview,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[JobItemView]:
    _require_sharing(settings)
    await _public_rate_limit(token, "import-review-batch", settings)
    _, _, job = await _recipient_job_context(
        token,
        job_id,
        request,
        session,
        settings,
    )
    if not body.item_ids:
        raise HTTPException(status_code=400, detail="Select at least one migration item")
    items = list(
        (
            await session.execute(
                select(orm.JobItem).where(
                    orm.JobItem.job_id == job_id,
                    orm.JobItem.id.in_(body.item_ids),
                )
            )
        ).scalars()
    )
    found_ids = {item.id for item in items}
    missing = [item_id for item_id in body.item_ids if item_id not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"migration item not found: {missing[0]}")
    return [
        await _apply_review(
            session,
            job,
            item,
            ReviewItem(action=body.action, target_uri=item.target_uri),
        )
        for item in items
    ]


@public_router.get("/{token}/imports/{job_id}/events")
async def recipient_import_events(
    token: str,
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    _require_sharing(settings)
    await _public_rate_limit(token, "import-events", settings)
    _, user_id, _ = await _recipient_job_context(
        token,
        job_id,
        request,
        session,
        settings,
    )
    return StreamingResponse(
        _event_stream(job_id, request, user_id=user_id),
        media_type="text/event-stream",
        headers=_public_headers(),
    )


@public_router.get("/{token}/download")
async def download_share(
    token: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    format_: Annotated[PortableFormat, Query(alias="format")] = PortableFormat.JSON,
) -> Response:
    _require_sharing(settings)
    await _public_rate_limit(token, f"download:{format_.value}", settings)
    share = await _load_public_or_http(session, token)
    try:
        exported = serialize_snapshot(
            _snapshot(share, max_bytes=settings.share_max_snapshot_bytes),
            format_,
            max_bytes=settings.share_max_download_bytes,
        )
    except SnapshotLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    headers = _public_headers()
    headers["Content-Disposition"] = f'attachment; filename="{exported.filename}"'
    return Response(
        content=exported.content,
        media_type=exported.media_type,
        headers=headers,
    )


@page_router.get("/share/{token}", response_class=HTMLResponse)
async def public_share_page(
    token: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    _require_sharing(settings)
    await _public_rate_limit(token, "page", settings)
    try:
        share = await load_public_share(session, token)
    except ShareNotFound:
        return _unavailable_page("Playlist share not found", status_code=404)
    except ShareUnavailable as exc:
        return _unavailable_page(f"This playlist share is {exc.reason}.", status_code=410)

    canonical = _share_url(settings, token)
    app_url = f"{settings.public_base_url_normalized}/shared/{token}"
    headers = _public_headers()
    headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    )
    return HTMLResponse(
        render_share_html(
            _snapshot(share, max_bytes=settings.share_max_snapshot_bytes),
            canonical_url=canonical,
            app_url=app_url,
            visibility=ShareVisibility(share.visibility),
        ),
        headers=headers,
    )


def _unavailable_page(message: str, *, status_code: int) -> HTMLResponse:
    headers = _public_headers()
    headers["Content-Security-Policy"] = "default-src 'none'; base-uri 'none'"
    return HTMLResponse(
        (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            "<title>Playlist unavailable</title></head><body>"
            f"<h1>Playlist unavailable</h1><p>{message}</p></body></html>"
        ),
        status_code=status_code,
        headers=headers,
    )
