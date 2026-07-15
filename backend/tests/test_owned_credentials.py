from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.adapter import AuthKind, ProviderCredential
from app.db.base import Base
from app.db.repositories import AccountNotFound, load_credential, save_credential


@pytest.mark.asyncio
async def test_credentials_cannot_be_loaded_across_user_boundaries() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with sessionmaker() as session:
        account = await save_credential(
            session,
            user_id="owner",
            provider="spotify",
            provider_user_id="provider-user",
            display_name="Owner",
            credential=ProviderCredential(
                provider="spotify",
                account_id="provider-user",
                auth_kind=AuthKind.OAUTH_PKCE,
                access_token="private-token",
            ),
        )
        await session.commit()

        credential, loaded = await load_credential(
            session,
            account_id=account.id,
            provider="spotify",
            user_id="owner",
        )
        assert loaded.user_id == "owner"
        assert credential.access_token == "private-token"

        with pytest.raises(AccountNotFound):
            await load_credential(
                session,
                account_id=account.id,
                provider="spotify",
                user_id="share-recipient:share:session",
            )

    await engine.dispose()
