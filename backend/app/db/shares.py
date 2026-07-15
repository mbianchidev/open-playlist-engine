"""Persistence helpers for immutable public playlist shares."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_scoped, encrypt_scoped
from app.core.sharing import (
    SNAPSHOT_SCHEMA_VERSION,
    SharedPlaylistSnapshot,
    ShareVisibility,
    generate_share_token,
    hash_share_token,
)
from app.db import models as orm


class ShareNotFound(Exception):
    pass


class ShareUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"playlist share is {reason}")


async def create_playlist_share(
    session: AsyncSession,
    *,
    owner_user_id: str,
    snapshot: SharedPlaylistSnapshot,
    visibility: ShareVisibility,
    expires_at: datetime | None,
) -> tuple[orm.PlaylistShare, str]:
    token = generate_share_token()
    snapshot_copy = json.loads(snapshot.model_dump_json())
    share = orm.PlaylistShare(
        owner_user_id=owner_user_id,
        token_hash=hash_share_token(token),
        enc_token=encrypt_scoped("playlist-share-token", token),
        visibility=visibility.value,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot=snapshot_copy,
        expires_at=expires_at,
    )
    session.add(share)
    await session.flush()
    return share, token


def decrypt_share_token(share: orm.PlaylistShare) -> str:
    return decrypt_scoped("playlist-share-token", share.enc_token)


async def load_public_share(
    session: AsyncSession,
    token: str,
    *,
    now: datetime | None = None,
    require_active: bool = True,
) -> orm.PlaylistShare:
    share = await session.scalar(
        select(orm.PlaylistShare).where(
            orm.PlaylistShare.token_hash == hash_share_token(token)
        )
    )
    if share is None:
        raise ShareNotFound(token)
    if require_active:
        reason = share_unavailable_reason(share, now=now)
        if reason:
            raise ShareUnavailable(reason)
    return share


def share_unavailable_reason(
    share: orm.PlaylistShare, *, now: datetime | None = None
) -> str | None:
    if share.revoked_at is not None:
        return "revoked"
    if share.expires_at is None:
        return None
    current = now or datetime.now(UTC)
    expires_at = share.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return "expired" if expires_at <= current else None


async def revoke_playlist_share(
    session: AsyncSession,
    share: orm.PlaylistShare,
    *,
    now: datetime | None = None,
) -> None:
    share.revoked_at = now or datetime.now(UTC)
    await session.flush()


async def save_recipient_auth_state(
    session: AsyncSession,
    *,
    state: str,
    share_id: str,
    recipient_user_id: str,
    provider: str,
    expires_at: datetime,
) -> orm.ShareRecipientAuthState:
    row = orm.ShareRecipientAuthState(
        share_id=share_id,
        state_hash=hash_share_token(state),
        recipient_user_id=recipient_user_id,
        provider=provider,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def consume_recipient_auth_state(
    session: AsyncSession,
    *,
    state: str | None,
    now: datetime | None = None,
) -> orm.ShareRecipientAuthState | None:
    if not state:
        return None
    row = await session.scalar(
        select(orm.ShareRecipientAuthState).where(
            orm.ShareRecipientAuthState.state_hash == hash_share_token(state)
        )
    )
    if row is None:
        return None
    current = now or datetime.now(UTC)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    await session.delete(row)
    await session.flush()
    return row if expires_at > current else None
