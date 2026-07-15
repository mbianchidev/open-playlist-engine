from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.adapter import AuthKind, ProviderCredential
from app.core.models import Playlist, Track
from app.core.rate_limit import rate_limiter
from app.core.sharing import ShareVisibility, build_shared_snapshot
from app.db.base import Base, get_session
from app.db.repositories import save_credential
from app.db.shares import create_playlist_share, revoke_playlist_share
from app.main import app
from app.settings import Settings, get_settings


class _FakeSource:
    async def read_playlist(self, credential, ref) -> Playlist:
        assert credential.access_token == "source-token"
        return Playlist(
            id=ref.id,
            name='Shared <script>alert("x")</script>',
            description="A safe immutable copy",
            owner_id="private-owner",
            snapshot_id="private-snapshot",
            tracks=[
                Track(
                    id="private-track",
                    title="Song",
                    artist="Artist",
                    provider_uris={"spotify": "spotify:track:track-id"},
                    metadata={"access_token": "secret"},
                )
            ],
        )


def _settings(**updates) -> Settings:
    values = {
        "public_base_url": "https://music.example",
        "frontend_url": "https://music.example",
        "secret_key": "s" * 64,
        "owner_access_token": "o" * 48,
        "share_rate_limit_capacity": 100,
        "share_rate_limit_refill_per_s": 100,
    }
    values.update(updates)
    return Settings(**values)


def test_owner_can_publish_inspect_copy_expire_and_revoke_share(monkeypatch) -> None:
    settings = _settings()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    account_id = asyncio.run(_seed_source_account(engine, sessionmaker))

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    from app.api import shares as shares_api

    monkeypatch.setattr(shares_api, "get", lambda provider: _FakeSource())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app, base_url=settings.public_base_url) as client:
            assert client.get("/api/shares").status_code == 401
            client.post("/api/session", json={"access_token": settings.owner_access_token})

            created = client.post(
                "/api/shares",
                json={
                    "provider": "spotify",
                    "account_id": account_id,
                    "playlist_id": "playlist-id",
                    "attribution": "Shared by Owner",
                    "visibility": "unlisted",
                    "expires_at": (
                        datetime.now(UTC) + timedelta(days=7)
                    ).isoformat(),
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["url"].startswith("https://music.example/share/")
            assert body["status"] == "active"
            assert body["snapshot"]["name"].startswith("Shared")
            assert "private-owner" not in created.text
            assert "private-snapshot" not in created.text
            assert "secret" not in created.text

            listed = client.get("/api/shares")
            assert listed.status_code == 200
            assert listed.json()[0]["url"] == body["url"]

            inspected = client.get(f"/api/shares/{body['id']}")
            assert inspected.status_code == 200
            assert inspected.json()["snapshot"]["tracks"][0]["source_url"].endswith(
                "/track/track-id"
            )

            expired = client.post(f"/api/shares/{body['id']}/expire")
            assert expired.status_code == 200
            assert expired.json()["status"] == "expired"
            assert client.get(body["url"].replace(settings.public_base_url, "")).status_code == 410

            revoked = client.post(f"/api/shares/{body['id']}/revoke")
            assert revoked.status_code == 200
            assert revoked.json()["status"] == "revoked"
    finally:
        app.dependency_overrides.clear()
        asyncio.run(rate_limiter.clear())
        asyncio.run(engine.dispose())


def test_public_snapshot_downloads_and_metadata_are_sanitized() -> None:
    settings = _settings()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    token = asyncio.run(_seed_share(engine, sessionmaker))

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app, base_url=settings.public_base_url) as client:
            public = client.get(f"/api/public/shares/{token}")
            assert public.status_code == 200
            assert public.headers["cache-control"] == "no-store"
            assert public.json()["snapshot"]["name"].startswith("Public")
            assert "private" not in public.text

            page = client.get(f"/share/{token}", follow_redirects=False)
            assert page.status_code == 200
            assert '<script>alert("x")</script>' not in page.text
            assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page.text
            assert "Track title" not in page.text
            assert "noindex,nofollow" in page.text

            for format_ in ("json", "csv", "txt", "m3u8", "xspf"):
                download = client.get(
                    f"/api/public/shares/{token}/download",
                    params={"format": format_},
                )
                assert download.status_code == 200, download.text
                assert "attachment;" in download.headers["content-disposition"]
                assert download.content
    finally:
        app.dependency_overrides.clear()
        asyncio.run(rate_limiter.clear())
        asyncio.run(engine.dispose())


def test_public_share_rate_limit_and_unavailable_states_are_clear() -> None:
    settings = _settings(
        share_rate_limit_capacity=1,
        share_rate_limit_refill_per_s=0.001,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    active_token, revoked_token, expired_token = asyncio.run(
        _seed_share_states(engine, sessionmaker)
    )

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app, base_url=settings.public_base_url) as client:
            assert client.get(f"/api/public/shares/{active_token}").status_code == 200
            limited = client.get(f"/api/public/shares/{active_token}")
            assert limited.status_code == 429
            assert int(limited.headers["retry-after"]) > 0

            revoked = client.get(f"/api/public/shares/{revoked_token}")
            assert revoked.status_code == 410
            assert revoked.json()["detail"] == "playlist share is revoked"

            expired = client.get(f"/api/public/shares/{expired_token}")
            assert expired.status_code == 410
            assert expired.json()["detail"] == "playlist share is expired"
    finally:
        app.dependency_overrides.clear()
        asyncio.run(rate_limiter.clear())
        asyncio.run(engine.dispose())


async def _seed_source_account(engine, sessionmaker) -> str:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessionmaker() as session:
        account = await save_credential(
            session,
            user_id="local",
            provider="spotify",
            provider_user_id="owner",
            display_name="Owner",
            credential=ProviderCredential(
                provider="spotify",
                account_id="owner",
                auth_kind=AuthKind.OAUTH_PKCE,
                access_token="source-token",
            ),
        )
        await session.commit()
        return account.id


def _public_snapshot():
    return build_shared_snapshot(
        Playlist(
            name='Public <script>alert("x")</script>',
            description="Description",
            owner_id="private-owner",
            tracks=[
                Track(
                    title="Track title",
                    artist="Artist",
                    metadata={"private": "value"},
                )
            ],
        ),
        provider="spotify",
        playlist_id="playlist",
        attribution="Owner",
        approved_artwork_hosts={"i.scdn.co"},
        max_tracks=10,
        max_bytes=100_000,
    )


async def _seed_share(engine, sessionmaker) -> str:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessionmaker() as session:
        _, token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_public_snapshot(),
            visibility=ShareVisibility.UNLISTED,
            expires_at=None,
        )
        await session.commit()
        return token


async def _seed_share_states(engine, sessionmaker) -> tuple[str, str, str]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessionmaker() as session:
        _, active_token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_public_snapshot(),
            visibility=ShareVisibility.UNLISTED,
            expires_at=None,
        )
        revoked, revoked_token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_public_snapshot(),
            visibility=ShareVisibility.UNLISTED,
            expires_at=None,
        )
        await revoke_playlist_share(session, revoked)
        _, expired_token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_public_snapshot(),
            visibility=ShareVisibility.PUBLIC,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()
        return active_token, revoked_token, expired_token
