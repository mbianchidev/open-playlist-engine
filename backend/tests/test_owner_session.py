from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base, get_session
from app.main import app
from app.settings import Settings, get_settings


def _configured_settings() -> Settings:
    return Settings(
        public_base_url="https://music.example",
        frontend_url="https://music.example",
        secret_key="s" * 64,
        owner_access_token="o" * 48,
    )


def test_owner_session_is_required_only_when_public_sharing_is_configured() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings()
    try:
        with TestClient(app, base_url="https://music.example") as client:
            response = client.get("/api/session")
            assert response.status_code == 200
            assert response.json() == {
                "required": False,
                "authenticated": True,
                "sharing_enabled": False,
                "sharing_disabled_reason": (
                    "Set OPE_PUBLIC_BASE_URL to enable public playlist sharing."
                ),
            }
    finally:
        app.dependency_overrides.clear()


def test_owner_login_uses_a_secure_http_only_cookie_and_rejects_tampering() -> None:
    settings = _configured_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, base_url="https://music.example") as client:
            status = client.get("/api/session")
            assert status.json()["authenticated"] is False

            denied = client.post("/api/session", json={"access_token": "wrong"})
            assert denied.status_code == 401
            assert settings.owner_access_token not in denied.text

            logged_in = client.post(
                "/api/session",
                json={"access_token": settings.owner_access_token},
            )
            assert logged_in.status_code == 200
            cookie = logged_in.headers["set-cookie"].lower()
            assert "httponly" in cookie
            assert "secure" in cookie
            assert "samesite=strict" in cookie
            assert client.get("/api/session").json()["authenticated"] is True

            client.cookies.set("ope_owner_session", "tampered")
            assert client.get("/api/session").json()["authenticated"] is False
    finally:
        app.dependency_overrides.clear()


def test_public_recipient_cookie_cannot_access_owner_accounts() -> None:
    settings = _configured_settings()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(_create_schema(engine))

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app, base_url="https://music.example") as client:
            client.cookies.set("ope_share_recipient", "recipient-cookie")
            denied = client.get("/api/auth/accounts")
            assert denied.status_code == 401

            client.post("/api/session", json={"access_token": settings.owner_access_token})
            allowed = client.get("/api/auth/accounts")
            assert allowed.status_code == 200
            assert allowed.json() == []
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_private_auth_and_playlist_openapi_no_longer_accepts_user_id() -> None:
    schema = app.openapi()
    paths = [
        ("/api/auth/{provider}/begin", "post"),
        ("/api/auth/accounts", "get"),
        ("/api/playlists", "get"),
        ("/api/playlists/{playlist_id}", "get"),
    ]
    for path, method in paths:
        parameters = schema["paths"][path][method].get("parameters", [])
        assert "user_id" not in {parameter["name"] for parameter in parameters}


async def _create_schema(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
