from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api import exports
from app.api.dependencies import get_current_user_id
from app.db.base import get_session
from app.exports.models import ExportWarning
from app.exports.service import ExportArtifact
from app.main import app


def test_export_routes_require_current_user_and_hide_user_id() -> None:
    routes = [
        route
        for route in exports.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/exports")
    ]

    assert routes
    for route in routes:
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        assert get_current_user_id in dependencies
        parameter_names = {parameter.name for parameter in route.dependant.query_params}
        assert "user_id" not in parameter_names


def test_export_openapi_declares_binary_download_content_types() -> None:
    operation = app.openapi()["paths"]["/api/exports"]["post"]
    content_types = operation["responses"]["200"]["content"]

    assert "text/csv" in content_types
    assert "application/vnd.apple.mpegurl" in content_types
    assert "application/xspf+xml" in content_types
    assert "application/vnd.open-playlist+json" in content_types
    assert "application/zip" in content_types


def test_live_export_download_streams_headers_and_removes_temp_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "playlist.csv"
    path.write_bytes(b"playlist export")

    async def fake_build(*args, **kwargs) -> ExportArtifact:
        return ExportArtifact(
            path=path,
            filename="Road-Trip.csv",
            media_type="text/csv; charset=utf-8",
            warnings=[
                ExportWarning(
                    code="unsupported_items",
                    message="One unsupported item",
                    playlist_id="playlist",
                )
            ],
        )

    session = AsyncMock()
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(exports, "_build_live_export", fake_build)
    try:
        response = client.post(
            "/api/exports",
            json={
                "source_provider": "spotify",
                "source_account_id": "account",
                "format": "csv",
                "selection": {"playlist_ids": ["playlist"], "tracks": {}},
            },
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 200
    assert response.content == b"playlist export"
    assert response.headers["content-disposition"].endswith("Road-Trip.csv")
    assert response.headers["x-open-playlist-warning-count"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert not path.exists()


@pytest.mark.asyncio
async def test_live_export_commits_refreshed_credentials_before_creating_temp_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.commit.side_effect = SQLAlchemyError("database unavailable")
    adapter = SimpleNamespace()
    build = AsyncMock()
    monkeypatch.setattr(exports, "get", lambda provider: adapter)
    monkeypatch.setattr(
        exports,
        "load_fresh_credential",
        AsyncMock(
            return_value=(
                SimpleNamespace(),
                SimpleNamespace(user_id="local"),
            )
        ),
    )
    monkeypatch.setattr(exports, "build_export_artifact", build)
    body = exports.CreateExport.model_validate(
        {
            "source_provider": "spotify",
            "source_account_id": "account",
            "format": "csv",
            "selection": {"playlist_ids": ["playlist"], "tracks": {}},
        }
    )

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await exports._build_live_export(body, session, user_id="local")

    build.assert_not_awaited()
