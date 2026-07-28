from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api import exports
from app.api.dependencies import get_current_user_id
from app.db import models as orm
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


@pytest.mark.asyncio
async def test_history_export_rejects_album_only_migration() -> None:
    session = AsyncMock()
    job = orm.MigrationJob(
        id="album-job",
        user_id="local",
        source_provider="spotify",
        target_provider="tidal",
        source_account_id="source",
        target_account_id="target",
        selection={
            "playlist_ids": [],
            "tracks": {},
            "saved_album_ids": ["album"],
            "followed_artist_ids": [],
        },
        status="done",
    )
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job)
    )

    with pytest.raises(HTTPException) as exc_info:
        await exports._build_history_export(
            "album-job",
            exports.CreateHistoryExport(format="json"),
            session,
            user_id="local",
        )

    assert exc_info.value.status_code == 400
    assert "no playlists" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_history_export_rejects_purged_item_details() -> None:
    session = AsyncMock()
    job = orm.MigrationJob(
        id="purged-job",
        user_id="local",
        source_provider="spotify",
        target_provider="tidal",
        source_account_id="source",
        target_account_id="target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
        status="done",
        details_purged_at=datetime.now(UTC),
    )
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job)
    )

    with pytest.raises(HTTPException) as exc_info:
        await exports._build_history_export(
            "purged-job",
            exports.CreateHistoryExport(format="json"),
            session,
            user_id="local",
        )

    assert exc_info.value.status_code == 410
    assert "expired" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_history_export_locks_job_until_artifact_is_built(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = AsyncMock()
    job = orm.MigrationJob(
        id="playlist-job",
        user_id="local",
        source_provider="spotify",
        target_provider="tidal",
        source_account_id="source",
        target_account_id="target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
        status="done",
    )
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job)
    )
    artifact = ExportArtifact(
        path=tmp_path / "history.json",
        filename="history.json",
        media_type="application/vnd.open-playlist+json",
    )
    build = AsyncMock(return_value=artifact)
    monkeypatch.setattr(exports, "build_export_artifact", build)

    result = await exports._build_history_export(
        "playlist-job",
        exports.CreateHistoryExport(format="json"),
        session,
        user_id="local",
    )

    statement = session.execute.await_args.args[0]
    assert "FOR UPDATE" in str(statement)
    build.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert result is artifact


@pytest.mark.asyncio
async def test_history_export_cleans_artifact_when_lock_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.commit.side_effect = SQLAlchemyError("database unavailable")
    job = orm.MigrationJob(
        id="playlist-job",
        user_id="local",
        source_provider="spotify",
        target_provider="tidal",
        source_account_id="source",
        target_account_id="target",
        selection={"playlist_ids": ["playlist"], "tracks": {}},
        status="done",
    )
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job)
    )
    artifact = SimpleNamespace(cleanup=Mock())
    monkeypatch.setattr(
        exports,
        "build_export_artifact",
        AsyncMock(return_value=artifact),
    )

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await exports._build_history_export(
            "playlist-job",
            exports.CreateHistoryExport(format="json"),
            session,
            user_id="local",
        )

    artifact.cleanup.assert_called_once()
