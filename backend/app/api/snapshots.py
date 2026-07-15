"""Local snapshot profiles, history, portable archives, and integrity checks."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import CurrentUserId
from app.core.capabilities import Capability
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session
from app.jobs.snapshot import run_snapshot
from app.settings import get_settings
from app.snapshots.bundle import (
    SCHEMA_VERSION,
    SnapshotCounts,
    SnapshotDiff,
    SnapshotError,
    SnapshotManifest,
)
from app.snapshots.service import (
    CleanupResult,
    cleanup_profile_snapshots,
    delete_snapshot_record,
    reconcile_snapshot_storage,
    snapshot_storage,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


class SnapshotProfileSourceInput(BaseModel):
    provider: str
    account_id: str
    collection_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_collections(self) -> SnapshotProfileSourceInput:
        self.collection_ids = list(dict.fromkeys(self.collection_ids))
        return self


class SnapshotProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sources: list[SnapshotProfileSourceInput] = Field(min_length=1)
    retention_count: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def unique_sources(self) -> SnapshotProfileCreate:
        keys = [(source.provider, source.account_id) for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("a provider account can appear only once in a snapshot profile")
        return self


class SnapshotProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    retention_count: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)


class SnapshotProfileSourceView(BaseModel):
    id: str
    provider: str
    account_id: str | None = None
    account_label: str | None = None
    collection_ids: list[str] = Field(default_factory=list)


class SnapshotProfileView(BaseModel):
    id: str
    name: str
    retention_count: int | None = None
    retention_days: int | None = None
    sources: list[SnapshotProfileSourceView] = Field(default_factory=list)
    snapshot_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SnapshotView(BaseModel):
    id: str
    profile_id: str | None = None
    profile_name: str | None = None
    bundle_id: str
    library_id: str
    source_providers: list[str] = Field(default_factory=list)
    source_labels: list[str] = Field(default_factory=list)
    status: str
    schema_version: int
    size_bytes: int = 0
    counts: SnapshotCounts = Field(default_factory=SnapshotCounts)
    errors: list[dict] = Field(default_factory=list)
    verification_status: str
    verification_error: str | None = None
    verified_at: datetime | None = None
    created_at: datetime | None = None


class SnapshotDetailView(SnapshotView):
    manifest: SnapshotManifest | None = None


class SnapshotListView(BaseModel):
    snapshots: list[SnapshotView] = Field(default_factory=list)
    total_bytes: int = 0


class SnapshotVerificationView(BaseModel):
    snapshot_id: str
    status: str
    archive_sha256: str | None = None
    verified_at: datetime | None = None
    error: str | None = None


class SnapshotCleanupView(BaseModel):
    deleted_count: int
    deleted_bytes: int

def _profile_view(
    profile: orm.SnapshotProfile,
    *,
    snapshot_count: int = 0,
) -> SnapshotProfileView:
    return SnapshotProfileView(
        id=profile.id,
        name=profile.name,
        retention_count=profile.retention_count,
        retention_days=profile.retention_days,
        sources=[
            SnapshotProfileSourceView(
                id=source.id,
                provider=source.provider,
                account_id=source.account_id,
                account_label=source.account_label,
                collection_ids=list(source.collection_ids or []),
            )
            for source in sorted(profile.sources, key=lambda item: (item.provider, item.id))
        ],
        snapshot_count=snapshot_count,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _snapshot_counts(row: orm.LibrarySnapshot) -> SnapshotCounts:
    raw_counts = row.manifest.get("counts") if isinstance(row.manifest, dict) else None
    return SnapshotCounts.model_validate(raw_counts or {})


def _snapshot_view(
    row: orm.LibrarySnapshot,
    *,
    profile_name: str | None = None,
) -> SnapshotView:
    return SnapshotView(
        id=row.id,
        profile_id=row.profile_id,
        profile_name=profile_name,
        bundle_id=row.bundle_id,
        library_id=row.library_id,
        source_providers=list(row.source_providers or []),
        source_labels=list(row.source_labels or []),
        status=row.status,
        schema_version=row.schema_version,
        size_bytes=row.size_bytes,
        counts=_snapshot_counts(row),
        errors=list(row.errors or []),
        verification_status=row.verification_status,
        verification_error=row.verification_error,
        verified_at=row.verified_at,
        created_at=row.created_at,
    )


def _snapshot_detail(
    row: orm.LibrarySnapshot,
    *,
    profile_name: str | None = None,
) -> SnapshotDetailView:
    manifest = (
        SnapshotManifest.model_validate(row.manifest)
        if isinstance(row.manifest, dict) and row.manifest
        else None
    )
    return SnapshotDetailView(
        **_snapshot_view(row, profile_name=profile_name).model_dump(),
        manifest=manifest,
    )


async def _owned_profile(
    session: AsyncSession,
    *,
    profile_id: str,
    user_id: str,
) -> orm.SnapshotProfile | None:
    return await session.scalar(
        select(orm.SnapshotProfile)
        .where(
            orm.SnapshotProfile.id == profile_id,
            orm.SnapshotProfile.user_id == user_id,
        )
        .options(selectinload(orm.SnapshotProfile.sources))
    )


async def _owned_snapshot(
    session: AsyncSession,
    *,
    snapshot_id: str,
    user_id: str,
    for_update: bool = False,
) -> orm.LibrarySnapshot | None:
    stmt = select(orm.LibrarySnapshot).where(
            orm.LibrarySnapshot.id == snapshot_id,
            orm.LibrarySnapshot.user_id == user_id,
        )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def _enqueue_or_inline(
    background_tasks: BackgroundTasks,
    snapshot_id: str,
) -> None:
    try:
        redis = await create_pool(RedisSettings.from_dsn(get_settings().valkey_url))
        try:
            await redis.enqueue_job("run_snapshot", snapshot_id)
        finally:
            await redis.close(close_connection_pool=True)
    except (ConnectionError, OSError, RedisError, TimeoutError) as exc:
        logger.warning(
            "queue unavailable; running snapshot inline snapshot_id=%s error=%s",
            snapshot_id,
            exc,
        )
        background_tasks.add_task(run_snapshot, {}, snapshot_id)


@router.get("/profiles", response_model=list[SnapshotProfileView])
async def list_snapshot_profiles(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[SnapshotProfileView]:
    profiles = list(
        (
            await session.execute(
                select(orm.SnapshotProfile)
                .where(orm.SnapshotProfile.user_id == user_id)
                .options(selectinload(orm.SnapshotProfile.sources))
                .order_by(orm.SnapshotProfile.created_at.desc(), orm.SnapshotProfile.id.desc())
            )
        ).scalars()
    )
    counts = dict(
        (
            await session.execute(
                select(
                    orm.LibrarySnapshot.profile_id,
                    func.count(orm.LibrarySnapshot.id),
                )
                .where(
                    orm.LibrarySnapshot.user_id == user_id,
                    orm.LibrarySnapshot.profile_id.is_not(None),
                )
                .group_by(orm.LibrarySnapshot.profile_id)
            )
        ).all()
    )
    return [
        _profile_view(profile, snapshot_count=int(counts.get(profile.id, 0)))
        for profile in profiles
    ]


@router.post("/profiles", response_model=SnapshotProfileView, status_code=201)
async def create_snapshot_profile(
    body: SnapshotProfileCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotProfileView:
    settings = get_settings()
    profile = orm.SnapshotProfile(
        user_id=user_id,
        name=body.name.strip(),
        retention_count=(
            body.retention_count
            if body.retention_count is not None
            else settings.snapshot_default_retention_count
        ),
        retention_days=(
            body.retention_days
            if body.retention_days is not None
            else settings.snapshot_default_retention_days
        ),
    )
    session.add(profile)
    await session.flush()
    for source_input in body.sources:
        try:
            adapter = get(source_input.provider)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        capabilities = adapter.info.capabilities
        if not (
            capabilities.can(Capability.READ_PLAYLISTS)
            and capabilities.can(Capability.READ_TRACKS)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"{adapter.info.display_name} cannot snapshot playlists",
            )
        account = await session.scalar(
            select(orm.ProviderAccount).where(
                orm.ProviderAccount.id == source_input.account_id,
                orm.ProviderAccount.user_id == user_id,
                orm.ProviderAccount.provider == source_input.provider,
            )
        )
        if account is None:
            raise HTTPException(
                status_code=404,
                detail=f"{source_input.provider} account was not found",
            )
        source = orm.SnapshotProfileSource(
            profile_id=profile.id,
            provider=source_input.provider,
            account_id=account.id,
            account_label=account.display_name,
            collection_ids=source_input.collection_ids,
        )
        session.add(source)
        profile.sources.append(source)
    await session.commit()
    return _profile_view(profile)


@router.patch("/profiles/{profile_id}", response_model=SnapshotProfileView)
async def update_snapshot_profile(
    profile_id: str,
    body: SnapshotProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotProfileView:
    profile = await _owned_profile(session, profile_id=profile_id, user_id=user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="snapshot profile not found")
    if "name" in body.model_fields_set and body.name is not None:
        profile.name = body.name.strip()
    if "retention_count" in body.model_fields_set:
        profile.retention_count = body.retention_count
    if "retention_days" in body.model_fields_set:
        profile.retention_days = body.retention_days
    await session.commit()
    return _profile_view(profile)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_snapshot_profile(
    profile_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> None:
    profile = await _owned_profile(session, profile_id=profile_id, user_id=user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="snapshot profile not found")
    await session.delete(profile)
    await session.commit()


@router.post(
    "/profiles/{profile_id}/snapshots",
    response_model=SnapshotView,
    status_code=202,
)
async def create_snapshot(
    profile_id: str,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotView:
    settings = get_settings()
    storage = snapshot_storage(settings)
    await reconcile_snapshot_storage(
        session,
        storage=storage,
        stale_after_s=settings.snapshot_stale_after_s,
    )
    profile = await _owned_profile(session, profile_id=profile_id, user_id=user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="snapshot profile not found")
    if not profile.sources:
        raise HTTPException(status_code=400, detail="snapshot profile has no sources")
    snapshot_id = str(uuid.uuid4())
    snapshot = orm.LibrarySnapshot(
        id=snapshot_id,
        user_id=user_id,
        profile_id=profile.id,
        bundle_id=snapshot_id,
        library_id=profile.id,
        source_providers=[source.provider for source in profile.sources],
        source_labels=[
            source.account_label
            for source in profile.sources
            if source.account_label
        ],
        status="pending",
        schema_version=SCHEMA_VERSION,
        size_bytes=0,
        manifest={},
        errors=[],
        verification_status="unverified",
    )
    session.add(snapshot)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="a snapshot is already pending or running for this profile",
        ) from exc
    await _enqueue_or_inline(background_tasks, snapshot.id)
    return _snapshot_view(snapshot, profile_name=profile.name)


@router.post(
    "/profiles/{profile_id}/cleanup",
    response_model=SnapshotCleanupView,
)
async def cleanup_snapshots(
    profile_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotCleanupView:
    profile = await _owned_profile(session, profile_id=profile_id, user_id=user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="snapshot profile not found")
    result: CleanupResult = await cleanup_profile_snapshots(
        session,
        profile=profile,
        storage=snapshot_storage(),
    )
    return SnapshotCleanupView(
        deleted_count=result.deleted_count,
        deleted_bytes=result.deleted_bytes,
    )


@router.post(
    "/import",
    response_model=SnapshotDetailView,
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/zip": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
            },
        }
    },
)
async def import_snapshot(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    confirm: bool = False,
) -> SnapshotDetailView:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required to import a portable snapshot",
        )
    storage = snapshot_storage()
    temp_path, archive_sha256, size_bytes = await storage.write_upload(request.stream())
    final_path = None
    try:
        duplicate = await session.scalar(
            select(orm.LibrarySnapshot.id).where(
                orm.LibrarySnapshot.user_id == user_id,
                orm.LibrarySnapshot.archive_sha256 == archive_sha256,
            )
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="this snapshot archive is already imported")
        verified = storage.verify_temp_archive(
            temp_path,
            expected_archive_sha256=archive_sha256,
        )
        local_id = str(uuid.uuid4())
        archive_name = f"{local_id}.opb"
        final_path = storage.adopt_temp_archive(temp_path, archive_name)
        row = orm.LibrarySnapshot(
            id=local_id,
            user_id=user_id,
            profile_id=None,
            bundle_id=verified.manifest.snapshot_id,
            library_id=verified.manifest.library_id,
            source_providers=[source.provider for source in verified.manifest.sources],
            source_labels=[
                source.account_label
                for source in verified.manifest.sources
                if source.account_label
            ],
            status=verified.manifest.status,
            schema_version=verified.manifest.schema_version,
            archive_name=archive_name,
            archive_sha256=archive_sha256,
            size_bytes=size_bytes,
            manifest=verified.manifest.model_dump(mode="json"),
            errors=[
                failure.model_dump(mode="json")
                for failure in verified.manifest.failures
            ],
            verification_status="verified",
            verification_error=None,
            verified_at=datetime.now(UTC),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            final_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail="this snapshot archive is already imported",
            ) from exc
        return _snapshot_detail(row)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    except SnapshotError as exc:
        temp_path.unlink(missing_ok=True)
        if final_path:
            final_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        temp_path.unlink(missing_ok=True)
        if final_path:
            final_path.unlink(missing_ok=True)
        raise


@router.get("", response_model=SnapshotListView)
async def list_snapshots(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    profile_id: str | None = None,
) -> SnapshotListView:
    stmt = select(orm.LibrarySnapshot).where(orm.LibrarySnapshot.user_id == user_id)
    if profile_id:
        stmt = stmt.where(orm.LibrarySnapshot.profile_id == profile_id)
    rows = list(
        (
            await session.execute(
                stmt.order_by(
                    orm.LibrarySnapshot.created_at.desc(),
                    orm.LibrarySnapshot.id.desc(),
                )
            )
        ).scalars()
    )
    profile_ids = {row.profile_id for row in rows if row.profile_id}
    profile_names = {}
    if profile_ids:
        profile_names = dict(
            (
                await session.execute(
                    select(orm.SnapshotProfile.id, orm.SnapshotProfile.name).where(
                        orm.SnapshotProfile.id.in_(profile_ids),
                        orm.SnapshotProfile.user_id == user_id,
                    )
                )
            ).all()
        )
    return SnapshotListView(
        snapshots=[
            _snapshot_view(row, profile_name=profile_names.get(row.profile_id))
            for row in rows
        ],
        total_bytes=sum(
            row.size_bytes
            for row in rows
            if row.archive_name and row.status in {"complete", "partial"}
        ),
    )


@router.get("/{snapshot_id}", response_model=SnapshotDetailView)
async def get_snapshot(
    snapshot_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotDetailView:
    row = await _owned_snapshot(session, snapshot_id=snapshot_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    profile_name = None
    if row.profile_id:
        profile_name = await session.scalar(
            select(orm.SnapshotProfile.name).where(
                orm.SnapshotProfile.id == row.profile_id,
                orm.SnapshotProfile.user_id == user_id,
            )
        )
    return _snapshot_detail(row, profile_name=profile_name)


@router.post("/{snapshot_id}/verify", response_model=SnapshotVerificationView)
async def verify_snapshot(
    snapshot_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotVerificationView:
    row = await _owned_snapshot(session, snapshot_id=snapshot_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if not row.archive_name:
        raise HTTPException(status_code=409, detail="snapshot archive is not available")
    try:
        verified = snapshot_storage().verify_archive(
            row.archive_name,
            expected_archive_sha256=row.archive_sha256,
        )
    except SnapshotError as exc:
        row.verification_status = "failed"
        row.verification_error = str(exc)
        row.verified_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.verification_status = "verified"
    row.verification_error = None
    row.verified_at = datetime.now(UTC)
    row.manifest = verified.manifest.model_dump(mode="json")
    await session.commit()
    return SnapshotVerificationView(
        snapshot_id=row.id,
        status="verified",
        archive_sha256=verified.archive_sha256,
        verified_at=row.verified_at,
    )


@router.get("/{snapshot_id}/diff", response_model=SnapshotDiff)
async def diff_snapshot(
    snapshot_id: str,
    base_snapshot_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> SnapshotDiff:
    compare = await _owned_snapshot(session, snapshot_id=snapshot_id, user_id=user_id)
    base = await _owned_snapshot(session, snapshot_id=base_snapshot_id, user_id=user_id)
    if compare is None or base is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if not compare.archive_name or not base.archive_name:
        raise HTTPException(status_code=409, detail="snapshot archive is not available")
    try:
        return snapshot_storage().diff_archives(base.archive_name, compare.archive_name)
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{snapshot_id}/download")
async def download_snapshot(
    snapshot_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> FileResponse:
    row = await _owned_snapshot(session, snapshot_id=snapshot_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if not row.archive_name:
        raise HTTPException(status_code=409, detail="snapshot archive is not available")
    try:
        path = snapshot_storage().archive_path(row.archive_name)
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="snapshot archive file is missing")
    timestamp = (row.created_at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"open-playlist-snapshot-{timestamp}-{row.bundle_id}.opb",
    )


@router.delete("/{snapshot_id}", status_code=204)
async def delete_snapshot(
    snapshot_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> None:
    row = await _owned_snapshot(
        session,
        snapshot_id=snapshot_id,
        user_id=user_id,
        for_update=True,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if row.status in {"pending", "running"}:
        raise HTTPException(status_code=409, detail="snapshot creation is still running")
    active_restore = await session.scalar(
        select(func.count())
        .select_from(orm.MigrationJob)
        .where(
            orm.MigrationJob.source_snapshot_id == row.id,
            orm.MigrationJob.status.in_(["pending", "running"]),
        )
    )
    if active_restore:
        raise HTTPException(status_code=409, detail="snapshot restore is still running")
    try:
        await delete_snapshot_record(session, row, snapshot_storage())
    except (OSError, SnapshotError) as exc:
        raise HTTPException(status_code=500, detail=f"could not delete snapshot: {exc}") from exc
