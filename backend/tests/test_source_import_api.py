"""API-level tests for the public URL / pasted-text preview routes
(``POST /api/imports/url-preview`` and ``POST /api/imports/text-preview``),
plus GET/DELETE-by-id coverage for URL/text-backed import records.

Binary local-file preview coverage lives in ``test_local_import_api.py``;
this file focuses on the generalized, non-file preview routes added by
issue #24 and their structured error mapping (task #7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.imports as imports_api
from app.db.base import Base, get_session
from app.imports import PASTED_TEXT_PROVIDER, PUBLIC_URL_PROVIDER
from app.imports.service import SourceConnectionRequired
from app.main import app
from app.settings import Settings, get_settings


@pytest.fixture
async def api_database(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source_imports.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
def import_settings() -> Settings:
    return Settings(
        local_import_max_bytes=1_000_000,
        local_import_max_playlists=10,
        local_import_max_tracks=100,
        local_import_max_issues=20,
        local_import_retention_s=3_600,
        local_import_queued_retention_s=7_200,
        local_import_failed_retention_s=600,
    )


@pytest.fixture
async def api_client(api_database, import_settings: Settings):
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with api_database() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: import_settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_text_preview_endpoint_persists_and_returns_source_preview(
    api_client: httpx.AsyncClient,
    api_database,
) -> None:
    response = await api_client.post(
        "/api/imports/text-preview",
        json={"text": "Beyonce - Deja Vu\nDaft Punk - One More Time", "name": "Road Trip"},
    )

    assert response.status_code == 201
    preview = response.json()
    assert preview["source_kind"] == PASTED_TEXT_PROVIDER
    assert preview["source_provider"] == PASTED_TEXT_PROVIDER
    assert preview["playlist"]["name"] == "Road Trip"
    assert preview["track_count"] == 2

    get_response = await api_client.get(f"/api/imports/{preview['id']}")
    assert get_response.status_code == 200

    delete_response = await api_client.delete(f"/api/imports/{preview['id']}")
    assert delete_response.status_code == 204
    assert (await api_client.get(f"/api/imports/{preview['id']}")).status_code == 404


async def test_text_preview_endpoint_rejects_empty_track_list(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/imports/text-preview",
        json={"text": "   \n   ", "name": None},
    )

    assert response.status_code == 400


async def test_url_preview_endpoint_maps_source_connection_required_to_409(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_url_import(session, *, user_id, url, source_account_id, settings):
        raise SourceConnectionRequired("spotify", "Connect Spotify to read this playlist URL.")

    monkeypatch.setattr(imports_api, "create_url_import", fake_create_url_import)

    response = await api_client.post(
        "/api/imports/url-preview",
        json={"url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "source_connection_required",
        "message": "Connect Spotify to read this playlist URL.",
        "provider": "spotify",
        "action": "connect_source",
    }


async def test_url_preview_endpoint_persists_resolved_playlist_and_supports_get_delete(
    api_client: httpx.AsyncClient,
    api_database,
    import_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.models import Playlist, Track
    from app.imports.external import ExternalImportResult
    from app.imports.service import create_source_import

    async def fake_create_url_import(session, *, user_id, url, source_account_id, settings):
        result = ExternalImportResult(
            source_provider="ytmusic",
            source_label="Road Trip (shared)",
            source_locator=url,
            source_fingerprint="fingerprint-1",
            playlist=Playlist(
                id="shared:playlist",
                name="Road Trip",
                tracks=[Track(id="one", title="Deja Vu", artist="Beyonce", source_item_id="s:1")],
            ),
        )
        return await create_source_import(
            session,
            user_id=user_id,
            source_kind=PUBLIC_URL_PROVIDER,
            result=result,
            settings=settings,
        )

    monkeypatch.setattr(imports_api, "create_url_import", fake_create_url_import)

    response = await api_client.post(
        "/api/imports/url-preview",
        json={"url": "https://music.youtube.com/playlist?list=abc"},
    )

    assert response.status_code == 201
    preview = response.json()
    assert preview["source_kind"] == PUBLIC_URL_PROVIDER
    assert preview["source_provider"] == "ytmusic"
    assert preview["playlist"]["tracks"][0]["title"] == "Deja Vu"

    get_response = await api_client.get(f"/api/imports/{preview['id']}")
    assert get_response.status_code == 200

    delete_response = await api_client.delete(f"/api/imports/{preview['id']}")
    assert delete_response.status_code == 204
