from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.adapter import (
    AuthChallenge,
    AuthKind,
    ChallengeShape,
    ProviderCredential,
)
from app.core.models import Playlist, Track
from app.core.rate_limit import rate_limiter
from app.core.sharing import ShareVisibility, build_shared_snapshot
from app.db import models as orm
from app.db.base import Base, get_session
from app.db.repositories import save_credential
from app.db.shares import create_playlist_share
from app.jobs import migration as migration_job
from app.main import app
from app.settings import Settings, get_settings
from tests.conformance.fake_provider import FakeAdapter


class _RedirectAuth:
    kind = AuthKind.OAUTH_PKCE

    def __init__(self) -> None:
        self.pending: dict[str, str] = {}

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        del account_label
        state = secrets.token_urlsafe(24)
        self.pending[state] = user_id
        return AuthChallenge(
            shape=ChallengeShape.REDIRECT,
            state=state,
            redirect_url=f"https://provider.example/authorize?state={state}",
        )

    async def complete(self, *, user_id: str, callback: dict) -> ProviderCredential:
        state = callback.get("state")
        assert self.pending.pop(state) == user_id
        return ProviderCredential(
            provider="fake",
            account_id="redirect-recipient",
            auth_kind=AuthKind.OAUTH_PKCE,
            access_token="recipient-token",
        )

    async def refresh(self, credential: ProviderCredential) -> ProviderCredential:
        return credential

    async def revoke(self, credential: ProviderCredential) -> None:
        return None


class _RedirectAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.auth = _RedirectAuth()


def _settings() -> Settings:
    return Settings(
        public_base_url="https://music.example",
        frontend_url="https://music.example",
        secret_key="s" * 64,
        owner_access_token="o" * 48,
        migration_safe_min_job_gap_s=0,
        share_rate_limit_capacity=100,
        share_rate_limit_refill_per_s=100,
    )


def _snapshot():
    return build_shared_snapshot(
        Playlist(
            name="Shared roadtrip",
            tracks=[
                Track(
                    title="Song One",
                    artist="Artist One",
                    isrc="US0000000001",
                    position=0,
                )
            ],
        ),
        provider="spotify",
        playlist_id="source-playlist",
        attribution="Friend",
        approved_artwork_hosts=set(),
        max_tracks=10,
        max_bytes=100_000,
    )


def test_recipient_accounts_are_share_scoped_and_owner_accounts_are_rejected(
    monkeypatch,
) -> None:
    settings = _settings()
    adapter = FakeAdapter()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    token, owner_account_id = asyncio.run(_seed_share_and_owner(engine, sessionmaker))

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    async def no_enqueue(background_tasks, job_id: str) -> None:
        del background_tasks, job_id

    from app.api import auth as auth_api
    from app.api import shares as shares_api

    monkeypatch.setattr(shares_api, "get", lambda provider: adapter)
    monkeypatch.setattr(auth_api, "get", lambda provider: adapter)
    monkeypatch.setattr(shares_api, "_enqueue_or_inline", no_enqueue)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app, base_url=settings.public_base_url) as client:
            accounts = client.get(f"/api/public/shares/{token}/accounts")
            assert accounts.status_code == 200
            assert accounts.json() == []
            cookie = accounts.headers["set-cookie"].lower()
            assert "httponly" in cookie
            assert "samesite=lax" in cookie

            challenge = client.post(f"/api/public/shares/{token}/auth/fake/begin")
            assert challenge.status_code == 200
            assert challenge.json()["shape"] == "form"

            completed = client.post(
                f"/api/public/shares/{token}/auth/fake/complete",
                json={"token": "recipient"},
            )
            assert completed.status_code == 200, completed.text
            recipient_account_id = completed.json()["account"]["id"]
            assert recipient_account_id != owner_account_id

            listed = client.get(f"/api/public/shares/{token}/accounts")
            assert [account["id"] for account in listed.json()] == [recipient_account_id]

            rejected = client.post(
                f"/api/public/shares/{token}/imports",
                json={
                    "target_provider": "fake",
                    "target_account_id": owner_account_id,
                },
            )
            assert rejected.status_code == 404

            imported = client.post(
                f"/api/public/shares/{token}/imports",
                json={
                    "target_provider": "fake",
                    "target_account_id": recipient_account_id,
                },
            )
            assert imported.status_code == 200, imported.text
            assert imported.json()["source_provider"] == "share"

            job = asyncio.run(_load_job(sessionmaker, imported.json()["id"]))
            assert job.user_id.startswith("share-recipient:")
            assert job.user_id != "local"
            assert job.target_account_id == recipient_account_id
            assert job.source_snapshot["name"] == "Shared roadtrip"
            assert "owner_account_id" not in str(job.source_snapshot)

            progress = client.get(
                f"/api/public/shares/{token}/imports/{imported.json()['id']}"
            )
            assert progress.status_code == 200
            assert progress.json()["id"] == imported.json()["id"]

            with TestClient(app, base_url=settings.public_base_url) as stranger:
                denied_progress = stranger.get(
                    f"/api/public/shares/{token}/imports/{imported.json()['id']}"
                )
                assert denied_progress.status_code == 401
    finally:
        app.dependency_overrides.clear()
        asyncio.run(rate_limiter.clear())
        asyncio.run(engine.dispose())


def test_redirect_callback_uses_persisted_recipient_state_without_owner_cookie(
    monkeypatch,
) -> None:
    settings = _settings()
    adapter = _RedirectAdapter()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    token, _ = asyncio.run(_seed_share_and_owner(engine, sessionmaker))

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    from app.api import auth as auth_api
    from app.api import shares as shares_api

    monkeypatch.setattr(shares_api, "get", lambda provider: adapter)
    monkeypatch.setattr(auth_api, "get", lambda provider: adapter)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app, base_url=settings.public_base_url) as client:
            client.get(f"/api/public/shares/{token}/accounts")
            begun = client.post(f"/api/public/shares/{token}/auth/fake/begin")
            assert begun.status_code == 200
            state = begun.json()["state"]

            callback = client.get(
                "/api/auth/fake/callback",
                params={"state": state, "code": "ok"},
            )
            assert callback.status_code == 200, callback.text
            assert f"/shared/{token}" in callback.text

            accounts = client.get(f"/api/public/shares/{token}/accounts")
            assert [account["provider_user_id"] for account in accounts.json()] == [
                "redirect-recipient"
            ]
    finally:
        app.dependency_overrides.clear()
        asyncio.run(rate_limiter.clear())
        asyncio.run(engine.dispose())


@pytest.mark.asyncio
async def test_snapshot_job_uses_recipient_target_without_loading_owner_source(
    monkeypatch,
) -> None:
    adapter = FakeAdapter()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    recipient_user_id = "share-recipient:share-id:session-id"
    async with sessionmaker() as session:
        share, _ = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_snapshot(),
            visibility=ShareVisibility.UNLISTED,
            expires_at=None,
        )
        account = await save_credential(
            session,
            user_id=recipient_user_id,
            provider="fake",
            provider_user_id="recipient",
            display_name="Recipient",
            credential=ProviderCredential(
                provider="fake",
                account_id="recipient",
                auth_kind=AuthKind.LONG_LIVED_TOKEN,
            ),
        )
        job = orm.MigrationJob(
            user_id=recipient_user_id,
            source_provider="share",
            source_account_id=share.id,
            source_share_id=share.id,
            source_snapshot=_snapshot().model_dump(mode="json"),
            target_provider="fake",
            target_account_id=account.id,
            selection={"playlist_ids": [share.id], "tracks": {}},
            status="pending",
            total=1,
        )
        session.add(job)
        await session.commit()

        def provider_get(provider: str):
            assert provider == "fake"
            return adapter

        monkeypatch.setattr(migration_job, "get", provider_get)
        await migration_job._run(session, job)
        await session.refresh(job)
        items = list(
            (
                await session.execute(
                    select(orm.JobItem).where(orm.JobItem.job_id == job.id)
                )
            ).scalars()
        )

        assert job.status == "done"
        assert len(items) == 1
        assert items[0].status == "written"
        assert adapter._created

    await engine.dispose()


async def _seed_share_and_owner(engine, sessionmaker) -> tuple[str, str]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessionmaker() as session:
        _, token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_snapshot(),
            visibility=ShareVisibility.UNLISTED,
            expires_at=None,
        )
        owner = await save_credential(
            session,
            user_id="local",
            provider="fake",
            provider_user_id="owner",
            display_name="Owner",
            credential=ProviderCredential(
                provider="fake",
                account_id="owner",
                auth_kind=AuthKind.LONG_LIVED_TOKEN,
            ),
        )
        await session.commit()
        return token, owner.id


async def _load_job(sessionmaker, job_id: str) -> orm.MigrationJob:
    async with sessionmaker() as session:
        job = await session.get(orm.MigrationJob, job_id)
        assert job is not None
        return job
