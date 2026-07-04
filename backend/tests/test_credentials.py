from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.core.adapter import AuthKind, ProviderCredential, RefreshTokenExpired
from app.db import repositories


class _ExpiredAuth:
    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        raise RefreshTokenExpired("spotify refresh token expired; reconnect Spotify")


class _ExpiredAdapter:
    auth = _ExpiredAuth()


class _RecordingSession:
    def __init__(self) -> None:
        self.deleted: list[object] = []
        self.flushed = False
        self.committed = False

    async def delete(self, row: object) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True


async def test_expired_refresh_token_discards_stored_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = SimpleNamespace(
        id="spotify-account",
        user_id="local",
        provider="spotify",
        provider_user_id="spotify-user",
        display_name="Spotify User",
    )
    credential = ProviderCredential(
        account_id=account.id,
        provider="spotify",
        auth_kind=AuthKind.OAUTH_PKCE,
        access_token="old-access-token",
        refresh_token="expired-refresh-token",
        expires_at=time.time() - 1,
    )

    async def load_credential(*args: object, **kwargs: object) -> tuple[ProviderCredential, object]:
        return credential, account

    monkeypatch.setattr(repositories, "load_credential", load_credential)
    session = _RecordingSession()

    with pytest.raises(RefreshTokenExpired):
        await repositories.load_fresh_credential(
            session, account_id=account.id, adapter=_ExpiredAdapter(), provider="spotify"
        )

    assert session.deleted == [account]
    assert session.flushed is True
    assert session.committed is True
