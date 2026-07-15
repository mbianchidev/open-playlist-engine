"""Self-hosted playlist generation, private drafts, and confirmation."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import CurrentUserId
from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    NotFound,
    ProviderAdapter,
    ProviderCredential,
    ProviderError,
    RateLimited,
    TrackCandidate,
)
from app.core.generator import (
    CopilotSDKModel,
    GeneratedTrackIntent,
    GenerationDraftNotConfirmable,
    GenerationSpec,
    GeneratorError,
    GeneratorInvalidOutput,
    GeneratorNotConfigured,
    GeneratorTimedOut,
    GeneratorUnavailable,
    OpenAICompatibleModel,
    PreferenceSummary,
    ResolvedGenerationItem,
    preference_summary_from_tracks,
    resolve_generated_plan,
)
from app.core.models import Playlist, Track
from app.core.preflight import validate_target_capabilities, write_preflight_warnings
from app.core.registry import get
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    load_fresh_credential,
)
from app.jobs.enqueue import enqueue_migration
from app.jobs.generator import build_confirmed_job
from app.settings import GeneratorBackend, Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/generator", tags=["generator"])
_MAX_LOCAL_SUMMARY_TRACKS = 500


class GeneratorLimitsView(BaseModel):
    max_prompt_chars: int
    max_output_chars: int
    max_tracks: int


class GeneratorConfigView(BaseModel):
    available: bool
    backend: GeneratorBackend
    model: str
    message: str
    limits: GeneratorLimitsView


class CreateGenerationDraft(BaseModel):
    target_provider: str
    target_account_id: str
    generation: GenerationSpec
    use_personalization: bool = False


class PreferenceUpdate(BaseModel):
    enabled: bool


class PreferenceView(BaseModel):
    enabled: bool
    summary: PreferenceSummary = Field(default_factory=PreferenceSummary)


class CandidateView(BaseModel):
    provider_track_id: str
    uri: str
    title: str
    artist: str
    album: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    explicit: bool | None = None
    market: str | None = None


class TrackSearchRequest(BaseModel):
    target_provider: str
    target_account_id: str
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    album: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=5, ge=1, le=10)


class CandidateSelection(BaseModel):
    uri: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    album: str | None = Field(default=None, max_length=200)


class DraftItemView(BaseModel):
    id: str
    position: int
    intent: GeneratedTrackIntent
    candidate: CandidateView | None = None
    confidence: float | None = None
    status: Literal["resolved", "needs_review", "unresolved"]
    reason: str | None = None


class DraftView(BaseModel):
    id: str
    status: str
    target_provider: str
    target_account_id: str
    name: str
    description: str | None = None
    model_backend: GeneratorBackend
    confirmed_job_id: str | None = None
    items: list[DraftItemView]
    playlist: Playlist


class UpdateDraft(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class AddDraftItem(BaseModel):
    candidate: CandidateSelection


class UpdateDraftItem(BaseModel):
    action: Literal["approve", "replace"]
    candidate: CandidateSelection | None = None


class ReorderDraftItems(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=50)


class ConfirmGenerationDraft(BaseModel):
    acknowledge_warnings: bool = False


class ConfirmationView(BaseModel):
    id: str
    status: str
    source_provider: str
    target_provider: str
    total: int = 0
    done: int = 0
    failed: int = 0
    error: str | None = None


@router.get("/config", response_model=GeneratorConfigView)
async def get_generator_config(
    settings: Annotated[Settings, Depends(get_settings)],
) -> GeneratorConfigView:
    if settings.generator_backend is GeneratorBackend.OPENAI_COMPATIBLE:
        available = bool(
            settings.generator_openai_base_url.strip() and settings.generator_model.strip()
        )
        message = (
            "Playlist generator is ready."
            if available
            else (
                "Playlist generation is disabled. Configure OPE_GENERATOR_OPENAI_BASE_URL "
                "and OPE_GENERATOR_MODEL."
            )
        )
    else:
        available = bool(settings.generator_model.strip())
        message = (
            "Playlist generator is ready through the GitHub Copilot SDK."
            if available
            else (
                "Playlist generation is disabled. Configure OPE_GENERATOR_MODEL and authenticate "
                "the GitHub Copilot SDK runtime."
            )
        )
    return GeneratorConfigView(
        available=available,
        backend=settings.generator_backend,
        model=settings.generator_model,
        message=message,
        limits=GeneratorLimitsView(
            max_prompt_chars=settings.generator_max_prompt_chars,
            max_output_chars=settings.generator_max_output_chars,
            max_tracks=settings.generator_max_tracks,
        ),
    )


@router.get("/preferences", response_model=PreferenceView)
async def get_preferences(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> PreferenceView:
    row = await session.get(orm.GenerationPreference, user_id)
    return _preference_view(row)


@router.put("/preferences", response_model=PreferenceView)
async def update_preferences(
    body: PreferenceUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> PreferenceView:
    row = await session.get(orm.GenerationPreference, user_id)
    if row is None:
        row = orm.GenerationPreference(user_id=user_id)
        session.add(row)
    row.enabled = body.enabled
    if body.enabled:
        row.summary = (await _build_local_preference_summary(session, user_id)).model_dump(
            mode="json"
        )
    await session.commit()
    return _preference_view(row)


@router.delete("/preferences", response_model=PreferenceView)
async def delete_preferences(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> PreferenceView:
    row = await session.get(orm.GenerationPreference, user_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return PreferenceView(enabled=False)


@router.post("/search", response_model=list[CandidateView])
async def search_target_tracks(
    body: TrackSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> list[CandidateView]:
    target, credential = await _target_context(
        session,
        provider=body.target_provider,
        account_id=body.target_account_id,
        user_id=user_id,
    )
    try:
        candidates = await target.search_tracks(
            credential,
            Track(title=body.title, artist=body.artist, album=body.album),
            limit=body.limit,
        )
    except ProviderError as exc:
        raise _provider_http_exception(exc) from exc
    return [_candidate_view(candidate) for candidate in _dedupe_candidates(candidates)]


@router.post("/drafts", response_model=DraftView)
async def create_draft(
    body: CreateGenerationDraft,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DraftView:
    target, credential = await _target_context(
        session,
        provider=body.target_provider,
        account_id=body.target_account_id,
        user_id=user_id,
    )
    try:
        validate_target_capabilities(
            target,
            credential,
            [Playlist(name="Generated playlist")],
        )
    except ProviderError as exc:
        raise _provider_http_exception(exc) from exc

    preference = await _generation_preference(
        session,
        user_id=user_id,
        enabled=body.use_personalization,
    )
    model = (
        OpenAICompatibleModel(settings)
        if settings.generator_backend is GeneratorBackend.OPENAI_COMPATIBLE
        else CopilotSDKModel(settings)
    )
    try:
        plan = await model.generate(body.generation, preference)
        resolved = await resolve_generated_plan(
            plan,
            target=target,
            credential=credential,
            review_threshold=settings.review_confidence_threshold,
        )
    except GeneratorError as exc:
        logger.warning(
            "playlist generation failed backend=%s error_type=%s",
            settings.generator_backend.value,
            type(exc).__name__,
        )
        raise _generator_http_exception(exc) from exc
    except ProviderError as exc:
        raise _provider_http_exception(exc) from exc

    draft = orm.GenerationDraft(
        user_id=user_id,
        target_provider=body.target_provider,
        target_account_id=body.target_account_id,
        name=plan.name,
        description=plan.description,
        model_backend=settings.generator_backend.value,
        status="draft",
    )
    session.add(draft)
    await session.flush()
    items = [
        _draft_item_from_resolution(draft.id, position, item)
        for position, item in enumerate(resolved)
    ]
    session.add_all(items)
    await session.commit()
    return _draft_view(draft, items)


@router.get("/drafts/{draft_id}", response_model=DraftView)
async def get_draft(
    draft_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> DraftView:
    draft = await _owned_draft(session, draft_id=draft_id, user_id=user_id)
    return _draft_view(draft, draft.items)


@router.patch("/drafts/{draft_id}", response_model=DraftView)
async def update_draft(
    draft_id: str,
    body: UpdateDraft,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> DraftView:
    draft = await _editable_draft(session, draft_id=draft_id, user_id=user_id)
    if body.name is not None:
        draft.name = body.name
    if "description" in body.model_fields_set:
        draft.description = body.description
    await session.commit()
    return _draft_view(draft, draft.items)


@router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(
    draft_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> None:
    draft = await _editable_draft(session, draft_id=draft_id, user_id=user_id)
    await session.delete(draft)
    await session.commit()


@router.post("/drafts/{draft_id}/items", response_model=DraftView)
async def add_draft_item(
    draft_id: str,
    body: AddDraftItem,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> DraftView:
    draft = await _editable_draft(session, draft_id=draft_id, user_id=user_id)
    target, credential = await _target_context(
        session,
        provider=draft.target_provider,
        account_id=draft.target_account_id,
        user_id=user_id,
    )
    candidate = await _validated_candidate(target, credential, body.candidate)
    if any(item.target_uri == candidate.uri for item in draft.items):
        raise HTTPException(status_code=409, detail="Track is already in this draft")
    position = max((item.position for item in draft.items), default=-1) + 1
    item = orm.GenerationDraftItem(
        draft_id=draft.id,
        position=position,
        intent_title=candidate.title,
        intent_artist=candidate.artist,
        intent_album=candidate.album,
        intent_reason="Added during review",
        provider_track_id=candidate.provider_track_id,
        target_uri=candidate.uri,
        resolved_title=candidate.title,
        resolved_artist=candidate.artist,
        resolved_album=candidate.album,
        duration_s=candidate.duration_s,
        isrc=candidate.isrc,
        explicit=candidate.explicit,
        confidence=1.0,
        status="resolved",
        reason="Selected from target provider search",
    )
    session.add(item)
    draft.items.append(item)
    await session.commit()
    return _draft_view(draft, draft.items)


@router.patch("/drafts/{draft_id}/items/{item_id}", response_model=DraftView)
async def update_draft_item(
    draft_id: str,
    item_id: str,
    body: UpdateDraftItem,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> DraftView:
    draft = await _editable_draft(session, draft_id=draft_id, user_id=user_id)
    item = _draft_item(draft, item_id)
    target, credential = await _target_context(
        session,
        provider=draft.target_provider,
        account_id=draft.target_account_id,
        user_id=user_id,
    )
    if body.action == "approve":
        if item.status != "needs_review" or not item.target_uri:
            raise HTTPException(status_code=400, detail="Only review matches can be approved")
        if not await target.validate_uri(credential, item.target_uri):
            raise HTTPException(status_code=400, detail="Target track is no longer valid")
        item.status = "resolved"
        item.reason = "Approved during generator review"
    else:
        if body.candidate is None:
            raise HTTPException(status_code=400, detail="candidate is required for replacement")
        candidate = await _validated_candidate(target, credential, body.candidate)
        if any(
            other.id != item.id and other.target_uri == candidate.uri for other in draft.items
        ):
            raise HTTPException(status_code=409, detail="Track is already in this draft")
        _apply_candidate(item, candidate)
    await session.commit()
    return _draft_view(draft, draft.items)


@router.delete("/drafts/{draft_id}/items/{item_id}", response_model=DraftView)
async def delete_draft_item(
    draft_id: str,
    item_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> DraftView:
    draft = await _editable_draft(session, draft_id=draft_id, user_id=user_id)
    item = _draft_item(draft, item_id)
    draft.items.remove(item)
    await session.delete(item)
    await _renumber_items(session, draft.items)
    await session.commit()
    return _draft_view(draft, draft.items)


@router.post("/drafts/{draft_id}/reorder", response_model=DraftView)
async def reorder_draft_items(
    draft_id: str,
    body: ReorderDraftItems,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
) -> DraftView:
    draft = await _editable_draft(session, draft_id=draft_id, user_id=user_id)
    existing = {item.id: item for item in draft.items}
    if len(set(body.item_ids)) != len(body.item_ids) or set(body.item_ids) != set(existing):
        raise HTTPException(
            status_code=400,
            detail="item_ids must contain every current draft item exactly once",
        )
    ordered = [existing[item_id] for item_id in body.item_ids]
    await _renumber_items(session, ordered)
    draft.items[:] = ordered
    await session.commit()
    return _draft_view(draft, ordered)


@router.post("/drafts/{draft_id}/confirm", response_model=ConfirmationView)
async def confirm_draft(
    draft_id: str,
    body: ConfirmGenerationDraft,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConfirmationView:
    draft = await _owned_draft(
        session,
        draft_id=draft_id,
        user_id=user_id,
        lock=True,
    )
    if draft.status != "draft":
        raise HTTPException(status_code=409, detail="Generation draft is already confirmed")
    target, credential = await _target_context(
        session,
        provider=draft.target_provider,
        account_id=draft.target_account_id,
        user_id=user_id,
    )
    items = sorted(draft.items, key=lambda value: value.position)
    _validate_confirmable_rows(items)
    for item in items:
        if not await target.validate_uri(credential, item.target_uri or ""):
            raise HTTPException(
                status_code=400,
                detail=(
                    f'Target track "{item.resolved_title or item.intent_title}" '
                    "is no longer valid"
                ),
            )
    playlist = _draft_playlist(draft, items)
    try:
        warnings = await write_preflight_warnings(
            session,
            settings=settings,
            user_id=user_id,
            target_provider=draft.target_provider,
            target_account_id=draft.target_account_id,
            target=target,
            target_credential=credential,
            playlists=[playlist],
        )
    except ProviderError as exc:
        raise _provider_http_exception(exc) from exc
    if warnings and not body.acknowledge_warnings:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "generation_warnings",
                "message": "Review and acknowledge warnings before creating the playlist.",
                "warnings": warnings,
            },
        )
    try:
        job, job_items = build_confirmed_job(draft, items)
    except GenerationDraftNotConfirmable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(job)
    session.add_all(job_items)
    await session.commit()
    await enqueue_migration(background_tasks, job.id)
    return ConfirmationView(
        id=job.id,
        status=job.status,
        source_provider=job.source_provider,
        target_provider=job.target_provider,
        total=job.total,
        done=job.done,
        failed=job.failed,
        error=job.error,
    )


async def _target_context(
    session: AsyncSession,
    *,
    provider: str,
    account_id: str,
    user_id: str,
) -> tuple[ProviderAdapter, ProviderCredential]:
    try:
        target = get(provider)
        credential, account = await load_fresh_credential(
            session,
            account_id=account_id,
            adapter=target,
            provider=provider,
        )
        if account.user_id != user_id:
            raise HTTPException(status_code=404, detail="Target provider account not found")
        return target, credential
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise _provider_http_exception(exc) from exc


def _provider_http_exception(exc: ProviderError) -> HTTPException:
    if isinstance(exc, AuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _generator_http_exception(exc: GeneratorError) -> HTTPException:
    if isinstance(exc, GeneratorTimedOut):
        return HTTPException(status_code=504, detail=str(exc))
    if isinstance(exc, (GeneratorNotConfigured, GeneratorUnavailable)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, GeneratorInvalidOutput):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail="Playlist generation failed")


async def _generation_preference(
    session: AsyncSession,
    *,
    user_id: str,
    enabled: bool,
) -> PreferenceSummary | None:
    if not enabled:
        return None
    row = await session.get(orm.GenerationPreference, user_id)
    if row is None or not row.enabled:
        raise HTTPException(
            status_code=400,
            detail="Enable local personalization before using it for generation",
        )
    try:
        return PreferenceSummary.model_validate(row.summary or {})
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored personalization data is invalid; reset it before generating",
        ) from exc


def _preference_view(row: orm.GenerationPreference | None) -> PreferenceView:
    if row is None:
        return PreferenceView(enabled=False)
    try:
        summary = PreferenceSummary.model_validate(row.summary or {})
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored personalization data is invalid; reset it before generating",
        ) from exc
    return PreferenceView(enabled=row.enabled, summary=summary)


async def _build_local_preference_summary(
    session: AsyncSession,
    user_id: str,
) -> PreferenceSummary:
    values: list[dict[str, object]] = []
    cached_rows = await session.scalars(
        select(orm.CachedPlaylistTracks.tracks)
        .where(orm.CachedPlaylistTracks.user_id == user_id)
        .order_by(orm.CachedPlaylistTracks.updated_at.desc())
        .limit(20)
    )
    for tracks in cached_rows:
        for track in tracks or []:
            if isinstance(track, dict):
                values.append(track)
                if len(values) >= _MAX_LOCAL_SUMMARY_TRACKS:
                    return preference_summary_from_tracks(values)

    remaining = _MAX_LOCAL_SUMMARY_TRACKS - len(values)
    if remaining > 0:
        metadata_rows = await session.scalars(
            select(orm.JobItem.source_metadata)
            .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
            .where(orm.MigrationJob.user_id == user_id)
            .order_by(orm.JobItem.updated_at.desc())
            .limit(remaining)
        )
        values.extend(value for value in metadata_rows if isinstance(value, dict))
    return preference_summary_from_tracks(values)


def _draft_item_from_resolution(
    draft_id: str,
    position: int,
    value: ResolvedGenerationItem,
) -> orm.GenerationDraftItem:
    candidate = value.candidate
    return orm.GenerationDraftItem(
        draft_id=draft_id,
        position=position,
        intent_title=value.intent.title,
        intent_artist=value.intent.artist,
        intent_album=value.intent.album,
        intent_reason=value.intent.reason,
        provider_track_id=candidate.provider_track_id if candidate else None,
        target_uri=candidate.uri if candidate else None,
        resolved_title=candidate.title if candidate else None,
        resolved_artist=candidate.artist if candidate else None,
        resolved_album=candidate.album if candidate else None,
        duration_s=candidate.duration_s if candidate else None,
        isrc=candidate.isrc if candidate else None,
        explicit=candidate.explicit if candidate else value.intent.explicit,
        confidence=value.confidence,
        status=value.status,
        reason=value.reason,
    )


async def _owned_draft(
    session: AsyncSession,
    *,
    draft_id: str,
    user_id: str,
    lock: bool = False,
) -> orm.GenerationDraft:
    stmt = (
        select(orm.GenerationDraft)
        .where(
            orm.GenerationDraft.id == draft_id,
            orm.GenerationDraft.user_id == user_id,
        )
        .options(selectinload(orm.GenerationDraft.items))
    )
    if lock:
        stmt = stmt.with_for_update()
    draft = await session.scalar(stmt)
    if draft is None:
        raise HTTPException(status_code=404, detail="Generation draft not found")
    return draft


async def _editable_draft(
    session: AsyncSession,
    *,
    draft_id: str,
    user_id: str,
) -> orm.GenerationDraft:
    draft = await _owned_draft(session, draft_id=draft_id, user_id=user_id, lock=True)
    if draft.status != "draft":
        raise HTTPException(status_code=409, detail="Confirmed generation drafts cannot be edited")
    return draft


def _draft_item(
    draft: orm.GenerationDraft,
    item_id: str,
) -> orm.GenerationDraftItem:
    item = next((value for value in draft.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Generation draft item not found")
    return item


async def _validated_candidate(
    target: ProviderAdapter,
    credential: ProviderCredential,
    selection: CandidateSelection,
) -> TrackCandidate:
    if not await target.validate_uri(credential, selection.uri):
        raise HTTPException(status_code=400, detail="Target track URI is invalid")
    try:
        candidates = await target.search_tracks(
            credential,
            Track(title=selection.title, artist=selection.artist, album=selection.album),
            limit=10,
        )
    except ProviderError as exc:
        raise _provider_http_exception(exc) from exc
    candidate = next((value for value in candidates if value.uri == selection.uri), None)
    if candidate is None:
        raise HTTPException(
            status_code=400,
            detail="Target track is no longer present in provider search results",
        )
    return candidate


def _apply_candidate(
    item: orm.GenerationDraftItem,
    candidate: TrackCandidate,
) -> None:
    item.provider_track_id = candidate.provider_track_id
    item.target_uri = candidate.uri
    item.resolved_title = candidate.title
    item.resolved_artist = candidate.artist
    item.resolved_album = candidate.album
    item.duration_s = candidate.duration_s
    item.isrc = candidate.isrc
    item.explicit = candidate.explicit
    item.confidence = 1.0
    item.status = "resolved"
    item.reason = "Selected from target provider search"


async def _renumber_items(
    session: AsyncSession,
    items: list[orm.GenerationDraftItem],
) -> None:
    for index, item in enumerate(items):
        item.position = -index - 1
    await session.flush()
    for index, item in enumerate(items):
        item.position = index


def _validate_confirmable_rows(items: list[orm.GenerationDraftItem]) -> None:
    if not items:
        raise HTTPException(status_code=400, detail="Add at least one track before confirmation")
    if any(item.status == "unresolved" or not item.target_uri for item in items):
        raise HTTPException(
            status_code=400,
            detail="Remove or replace unresolved tracks before confirmation",
        )
    if any(item.status == "needs_review" for item in items):
        raise HTTPException(
            status_code=400,
            detail="Approve or replace every match that needs review before confirmation",
        )
    if len({item.target_uri for item in items}) != len(items):
        raise HTTPException(status_code=400, detail="Remove duplicate tracks before confirmation")


def _draft_view(
    draft: orm.GenerationDraft,
    items: list[orm.GenerationDraftItem],
) -> DraftView:
    ordered = sorted(items, key=lambda value: value.position)
    try:
        backend = GeneratorBackend(draft.model_backend)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Generation draft backend is invalid") from exc
    return DraftView(
        id=draft.id,
        status=draft.status,
        target_provider=draft.target_provider,
        target_account_id=draft.target_account_id,
        name=draft.name,
        description=draft.description,
        model_backend=backend,
        confirmed_job_id=draft.confirmed_job_id,
        items=[_draft_item_view(item) for item in ordered],
        playlist=_draft_playlist(draft, ordered),
    )


def _draft_item_view(item: orm.GenerationDraftItem) -> DraftItemView:
    candidate = None
    if item.target_uri and item.provider_track_id and item.resolved_title and item.resolved_artist:
        candidate = CandidateView(
            provider_track_id=item.provider_track_id,
            uri=item.target_uri,
            title=item.resolved_title,
            artist=item.resolved_artist,
            album=item.resolved_album,
            duration_s=item.duration_s,
            isrc=item.isrc,
            explicit=item.explicit,
        )
    return DraftItemView(
        id=item.id,
        position=item.position,
        intent=GeneratedTrackIntent(
            title=item.intent_title,
            artist=item.intent_artist,
            album=item.intent_album,
            explicit=item.explicit,
            reason=item.intent_reason,
        ),
        candidate=candidate,
        confidence=item.confidence,
        status=item.status,
        reason=item.reason,
    )


def _draft_playlist(
    draft: orm.GenerationDraft,
    items: list[orm.GenerationDraftItem],
) -> Playlist:
    return Playlist(
        id=draft.id,
        name=draft.name,
        description=draft.description,
        tracks=[
            Track(
                id=item.provider_track_id,
                title=item.resolved_title or item.intent_title,
                artist=item.resolved_artist or item.intent_artist,
                album=item.resolved_album or item.intent_album,
                duration_s=item.duration_s,
                explicit=item.explicit,
                isrc=item.isrc,
                provider_uris=(
                    {draft.target_provider: item.target_uri} if item.target_uri else {}
                ),
                metadata={
                    "generation_item_id": item.id,
                    "generation_status": item.status,
                    "confidence": item.confidence,
                },
                position=item.position,
                unsupported_reason=item.reason if item.status == "unresolved" else None,
            )
            for item in items
        ],
    )


def _dedupe_candidates(candidates: list[TrackCandidate]) -> list[TrackCandidate]:
    output: list[TrackCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.uri.strip().casefold() or candidate.provider_track_id.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def _candidate_view(candidate: TrackCandidate) -> CandidateView:
    return CandidateView(
        provider_track_id=candidate.provider_track_id,
        uri=candidate.uri,
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
        duration_s=candidate.duration_s,
        isrc=candidate.isrc,
        explicit=candidate.explicit,
        market=candidate.market,
    )
