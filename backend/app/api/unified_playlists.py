"""Provider-neutral playlist library assembled from connected accounts."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserId
from app.api.playlists import _playlist_detail, _playlist_refs
from app.core.adapter import AuthExpired, ProviderError
from app.core.registry import get
from app.core.unified_playlists import (
    ProviderPlaylist,
    SyncLink,
    UnifiedPlaylist,
    UnifiedSyncAttempt,
    build_unified_playlists,
)
from app.db import models as orm
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    list_accounts,
    load_fresh_credential,
)

router = APIRouter(prefix="/api/unified-playlists", tags=["unified playlists"])
logger = logging.getLogger(__name__)


class UnifiedPlaylistWarning(BaseModel):
    scope: Literal["account", "playlist"]
    provider: str
    account_id: str
    playlist_id: str | None = None
    message: str
    reconnect_required: bool = False


class UnifiedPlaylistLibrary(BaseModel):
    playlists: list[UnifiedPlaylist] = Field(default_factory=list)
    warnings: list[UnifiedPlaylistWarning] = Field(default_factory=list)
    scanned_account_count: int
    connected_provider_count: int


@router.get("", response_model=UnifiedPlaylistLibrary)
async def list_unified_playlists(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: CurrentUserId,
    refresh: Annotated[bool, Query()] = False,
) -> UnifiedPlaylistLibrary:
    accounts = await list_accounts(session, user_id=user_id)
    provider_playlists: list[ProviderPlaylist] = []
    warnings: list[UnifiedPlaylistWarning] = []

    for account in accounts:
        try:
            adapter = get(account.provider)
            credential, _ = await load_fresh_credential(
                session,
                account_id=account.id,
                adapter=adapter,
                provider=account.provider,
                user_id=user_id,
            )
            refs = await _playlist_refs(
                session,
                adapter=adapter,
                credential=credential,
                user_id=user_id,
                provider=account.provider,
                account_id=account.id,
                refresh=refresh,
            )
        except (KeyError, AccountNotFound, CredentialNotFound, ProviderError) as exc:
            reconnect_required = isinstance(exc, (AccountNotFound, CredentialNotFound, AuthExpired))
            logger.warning(
                "unified playlist account scan failed provider=%s account_id=%s error=%s",
                account.provider,
                account.id,
                exc,
            )
            warnings.append(
                UnifiedPlaylistWarning(
                    scope="account",
                    provider=account.provider,
                    account_id=account.id,
                    message=str(exc),
                    reconnect_required=reconnect_required,
                )
            )
            continue

        for ref in refs:
            try:
                playlist = await _playlist_detail(
                    session,
                    adapter=adapter,
                    credential=credential,
                    user_id=user_id,
                    provider=account.provider,
                    account_id=account.id,
                    playlist_id=ref.id,
                    refresh=refresh,
                )
            except ProviderError as exc:
                logger.warning(
                    "unified playlist read failed provider=%s account_id=%s "
                    "playlist_id=%s error=%s",
                    account.provider,
                    account.id,
                    ref.id,
                    exc,
                )
                warnings.append(
                    UnifiedPlaylistWarning(
                        scope="playlist",
                        provider=account.provider,
                        account_id=account.id,
                        playlist_id=ref.id,
                        message=str(exc),
                        reconnect_required=isinstance(exc, AuthExpired),
                    )
                )
                continue
            provider_playlists.append(
                ProviderPlaylist(
                    provider=account.provider,
                    account_id=account.id,
                    account_label=account.display_name or account.provider,
                    playlist=playlist,
                )
            )

    sync_rows = list(
        (
            await session.execute(select(orm.SyncRule).where(orm.SyncRule.user_id == user_id))
        ).scalars()
    )
    valid_rule_ids = {row.id for row in sync_rows}
    sync_links = [
        SyncLink(
            source_provider=row.source_provider,
            source_account_id=row.source_account_id,
            source_playlist_id=row.source_playlist_id,
            target_provider=row.target_provider,
            target_account_id=row.target_account_id,
            target_playlist_id=row.target_playlist_id,
            rule_id=row.id,
            enabled=row.enabled,
            status=row.status,
        )
        for row in sync_rows
    ]
    job_rows = list(
        (
            await session.execute(
                select(orm.MigrationJob)
                .where(
                    orm.MigrationJob.user_id == user_id,
                    orm.MigrationJob.origin == "manual",
                )
                .order_by(orm.MigrationJob.created_at, orm.MigrationJob.id)
            )
        ).scalars()
    )
    attempts_by_source: dict[str, dict[str, UnifiedSyncAttempt]] = {}
    for job in job_rows:
        selection = job.selection if isinstance(job.selection, dict) else {}
        intent = selection.get("continuous_sync")
        playlist_ids = selection.get("playlist_ids")
        if not isinstance(intent, dict) or not isinstance(playlist_ids, list):
            continue
        if len(playlist_ids) != 1:
            continue
        source_key = f"{job.source_provider}:{job.source_account_id}:{playlist_ids[0]}"
        intent_status = intent.get("status")
        sync_rule_id = intent.get("sync_rule_id")
        removed_rule = (
            intent_status == "active"
            and isinstance(sync_rule_id, str)
            and sync_rule_id not in valid_rule_ids
        )
        if intent_status == "active" and sync_rule_id in valid_rule_ids:
            status = "active"
        elif removed_rule or intent_status == "failed" or job.status == "failed":
            status = "failed"
        else:
            status = "pending"
        attempts_by_source.setdefault(source_key, {})[job.target_account_id] = UnifiedSyncAttempt(
            migration_job_id=job.id,
            source_member_key=source_key,
            target_provider=job.target_provider,
            target_account_id=job.target_account_id,
            status=status,
            sync_rule_id=(
                str(sync_rule_id) if isinstance(sync_rule_id, str) and not removed_rule else None
            ),
            error=(
                "continuous sync rule was removed"
                if removed_rule
                else str(intent["error"])
                if isinstance(intent.get("error"), str)
                else job.error
                if job.status == "failed"
                else None
            ),
        )

    unified = build_unified_playlists(provider_playlists, sync_links=sync_links)
    for index, playlist in enumerate(unified):
        latest_by_target: dict[str, UnifiedSyncAttempt] = {}
        for member in playlist.members:
            latest_by_target.update(attempts_by_source.get(member.key, {}))
        unified[index] = playlist.model_copy(
            update={
                "sync_attempts": sorted(
                    latest_by_target.values(),
                    key=lambda attempt: (
                        attempt.target_provider,
                        attempt.target_account_id,
                    ),
                )
            }
        )

    await session.commit()
    return UnifiedPlaylistLibrary(
        playlists=unified,
        warnings=warnings,
        scanned_account_count=len(accounts),
        connected_provider_count=len({account.provider for account in accounts}),
    )
