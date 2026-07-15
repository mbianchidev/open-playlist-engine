from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.api.imports import get_import_service
from app.db import models as orm
from app.db.base import get_session
from app.imports.service import SourceConnectionRequired
from app.main import app


class _Session:
    committed = False

    async def commit(self) -> None:
        self.committed = True


class _Service:
    async def preview_text(self, session, *, user_id: str, text: str, name: str | None):
        return orm.ImportedPlaylist(
            id="import-1",
            user_id=user_id,
            source_provider="text",
            source_label="Pasted text",
            source_locator="text:fingerprint",
            source_fingerprint="fingerprint",
            playlist_id="text:fingerprint",
            playlist={
                "id": "text:fingerprint",
                "name": name or "Imported track list",
                "tracks": [{"title": "Song", "artist": "Artist"}],
            },
            issues=[],
        )

    async def preview_url(self, session, *, user_id: str, url: str, source_account_id: str | None):
        raise SourceConnectionRequired(
            "spotify",
            "Connect Spotify to read this playlist URL.",
        )


def test_import_preview_endpoint_returns_playlist_and_snapshot_id() -> None:
    session = _Session()

    async def session_override() -> AsyncIterator[_Session]:
        yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_import_service] = lambda: _Service()
    try:
        response = TestClient(app).post(
            "/api/imports/preview",
            json={"kind": "text", "text": "Artist - Song", "name": "List"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["import_id"] == "import-1"
    assert response.json()["playlist"]["name"] == "List"
    assert response.json()["track_count"] == 1
    assert session.committed is True


def test_import_preview_endpoint_returns_structured_connection_action() -> None:
    async def session_override() -> AsyncIterator[_Session]:
        yield _Session()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_import_service] = lambda: _Service()
    try:
        response = TestClient(app).post(
            "/api/imports/preview",
            json={
                "kind": "url",
                "url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "source_connection_required",
        "message": "Connect Spotify to read this playlist URL.",
        "provider": "spotify",
        "action": "connect_source",
    }
