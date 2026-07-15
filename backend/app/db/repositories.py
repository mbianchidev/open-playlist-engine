"""Small persistence helpers for account credentials and migration jobs."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import ProviderAdapter, RefreshTokenExpired
from app.core.adapter import ProviderCredential as RuntimeCredential
from app.core.security import decrypt, encrypt
from app.db import models as orm

logger = logging.getLogger(__name__)


class AccountNotFound(Exception):
    pass


class CredentialNotFound(Exception):
    pass


def _to_datetime(epoch: float | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def _to_epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    return value.timestamp()


async def list_accounts(
    session: AsyncSession, *, user_id: str, provider: str | None = None
) -> list[orm.ProviderAccount]:
    stmt = select(orm.ProviderAccount).where(orm.ProviderAccount.user_id == user_id)
    if provider:
        stmt = stmt.where(orm.ProviderAccount.provider == provider)
    stmt = stmt.order_by(orm.ProviderAccount.created_at.desc())
    return list((await session.execute(stmt)).scalars())


async def delete_account(session: AsyncSession, *, account_id: str, user_id: str) -> bool:
    stmt = select(orm.ProviderAccount).where(
        orm.ProviderAccount.id == account_id,
        orm.ProviderAccount.user_id == user_id,
    )
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None:
        return False
    await session.delete(account)
    await session.flush()
    return True


async def save_credential(
    session: AsyncSession,
    *,
    user_id: str,
    provider: str,
    provider_user_id: str | None,
    display_name: str | None,
    credential: RuntimeCredential,
) -> orm.ProviderAccount:
    stmt = select(orm.ProviderAccount).where(
        orm.ProviderAccount.user_id == user_id,
        orm.ProviderAccount.provider == provider,
        orm.ProviderAccount.provider_user_id == provider_user_id,
    )
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None:
        account = orm.ProviderAccount(
            user_id=user_id,
            provider=provider,
            provider_user_id=provider_user_id,
            display_name=display_name,
        )
        session.add(account)
        await session.flush()
    else:
        account.display_name = display_name or account.display_name

    persisted = credential.model_copy(update={"account_id": account.id, "provider": provider})
    blob = json.dumps(persisted.model_dump(mode="json"))
    session.add(
        orm.ProviderCredential(
            account_id=account.id,
            auth_kind=persisted.auth_kind.value,
            enc_blob=encrypt(blob),
            scopes=persisted.scopes,
            expires_at=_to_datetime(persisted.expires_at),
            version=persisted.version,
        )
    )
    await session.flush()
    return account


async def load_credential(
    session: AsyncSession, *, account_id: str, provider: str | None = None
) -> tuple[RuntimeCredential, orm.ProviderAccount]:
    stmt = select(orm.ProviderAccount).where(orm.ProviderAccount.id == account_id)
    if provider:
        stmt = stmt.where(orm.ProviderAccount.provider == provider)
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None:
        raise AccountNotFound(account_id)

    cred_stmt = (
        select(orm.ProviderCredential)
        .where(orm.ProviderCredential.account_id == account.id)
        .order_by(orm.ProviderCredential.created_at.desc(), orm.ProviderCredential.id.desc())
        .limit(1)
    )
    row = (await session.execute(cred_stmt)).scalar_one_or_none()
    if row is None:
        raise CredentialNotFound(account.id)

    payload = json.loads(decrypt(row.enc_blob))
    extra = dict(payload.get("extra") or {})
    if account.provider_user_id:
        extra.setdefault("provider_user_id", account.provider_user_id)
    credential = RuntimeCredential.model_validate(payload).model_copy(
        update={
            "account_id": account.id,
            "provider": account.provider,
            "scopes": list(row.scopes or []),
            "expires_at": _to_epoch(row.expires_at),
            "extra": extra,
            "version": row.version,
        }
    )
    return credential, account


async def load_fresh_credential(
    session: AsyncSession,
    *,
    account_id: str,
    adapter: ProviderAdapter,
    provider: str | None = None,
) -> tuple[RuntimeCredential, orm.ProviderAccount]:
    credential, account = await load_credential(session, account_id=account_id, provider=provider)
    if credential.expires_at and credential.expires_at <= time.time() + 60:
        try:
            refreshed = await adapter.auth.refresh(credential)
        except RefreshTokenExpired:
            logger.info(
                "discarding expired refresh token provider=%s account_id=%s",
                account.provider,
                account.id,
            )
            await session.delete(account)
            await session.flush()
            await session.commit()
            raise
        account = await save_credential(
            session,
            user_id=account.user_id,
            provider=account.provider,
            provider_user_id=account.provider_user_id,
            display_name=account.display_name,
            credential=refreshed,
        )
        credential = refreshed.model_copy(
            update={"account_id": account.id, "provider": account.provider}
        )
    return credential, account


async def invalidate_playlist_cache(
    session: AsyncSession,
    *,
    user_id: str,
    provider: str,
    account_id: str,
) -> None:
    conditions = (
        orm.CachedPlaylistRef.user_id == user_id,
        orm.CachedPlaylistRef.provider == provider,
        orm.CachedPlaylistRef.account_id == account_id,
    )
    await session.execute(
        delete(orm.CachedPlaylistTracks).where(
            orm.CachedPlaylistTracks.user_id == user_id,
            orm.CachedPlaylistTracks.provider == provider,
            orm.CachedPlaylistTracks.account_id == account_id,
        )
    )
    await session.execute(delete(orm.CachedPlaylistRef).where(*conditions))
    await session.flush()
