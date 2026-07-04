from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.core.adapter import AuthExpired, AuthKind, ProviderCredential
from app.db import repositories


class _Session:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class _ExpiredAuth:
    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        raise AuthExpired("spotify refresh token expired; reconnect Spotify")


class _ExpiredAdapter:
    auth = _ExpiredAuth()


async def test_load_fresh_credential_discards_account_when_refresh_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = SimpleNamespace(
        id="account-id",
        user_id="user-id",
        provider="spotify",
        provider_user_id="spotify-user",
        display_name="Spotify User",
    )
    credential = ProviderCredential(
        account_id=account.id,
        provider=account.provider,
        auth_kind=AuthKind.OAUTH_PKCE,
        access_token="old-access-token",
        refresh_token="expired-refresh-token",
        expires_at=time.time() - 10,
    )
    deleted: dict[str, str] = {}

    async def fake_load_credential(session, *, account_id: str, provider: str | None = None):
        return credential, account

    async def fake_delete_account(session, *, account_id: str, user_id: str) -> bool:
        deleted["account_id"] = account_id
        deleted["user_id"] = user_id
        return True

    async def fail_save_credential(*args, **kwargs):
        pytest.fail("expired credentials must be discarded, not saved")

    monkeypatch.setattr(repositories, "load_credential", fake_load_credential)
    monkeypatch.setattr(repositories, "delete_account", fake_delete_account)
    monkeypatch.setattr(repositories, "save_credential", fail_save_credential)

    session = _Session()
    with pytest.raises(AuthExpired):
        await repositories.load_fresh_credential(
            session,
            account_id=account.id,
            adapter=_ExpiredAdapter(),
            provider=account.provider,
        )

    assert deleted == {"account_id": account.id, "user_id": account.user_id}
    assert session.committed is True
