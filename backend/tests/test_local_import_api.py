from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies import get_current_user_id
from app.api.imports import router
from app.db import models as orm
from app.db.base import Base, get_session
from app.imports.models import ImportLimits
from app.imports.service import (
    cleanup_expired_imports,
    queue_import,
    spool_upload,
)
from app.main import app
from app.settings import Settings, get_settings

FIXTURES = Path(__file__).parent / "fixtures" / "local_imports"


@pytest.fixture
async def api_database(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'imports.db'}")
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
        local_import_spool_memory_bytes=128,
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


async def test_preview_get_and_discard_import(
    api_client: httpx.AsyncClient,
    api_database,
) -> None:
    response = await api_client.post(
        "/api/imports/preview",
        params={"filename": r"C:\fakepath\road-trip.csv"},
        content=(FIXTURES / "valid.csv").read_bytes(),
        headers={"content-type": "text/csv"},
    )

    assert response.status_code == 201
    preview = response.json()
    assert preview["filename"] == "road-trip.csv"
    assert preview["detected_format"] == "csv"
    assert preview["playlist_count"] == 1
    assert preview["track_count"] == 4
    assert preview["duplicate_count"] == 1
    assert preview["malformed_count"] == 1
    assert preview["unsupported_count"] == 2
    assert preview["playlists"][0]["tracks"][0]["source_item_id"]

    get_response = await api_client.get(f"/api/imports/{preview['id']}")
    assert get_response.status_code == 200
    assert get_response.json() == preview

    async with api_database() as session:
        record = await session.scalar(
            select(orm.LocalPlaylistImport).where(orm.LocalPlaylistImport.id == preview["id"])
        )
        assert record is not None
        assert not hasattr(record, "path")
        assert record.playlists[0]["tracks"][0]["title"] == "Déjà Vu"

    delete_response = await api_client.delete(f"/api/imports/{preview['id']}")
    assert delete_response.status_code == 204
    assert (await api_client.get(f"/api/imports/{preview['id']}")).status_code == 404


async def test_preview_rejects_content_length_before_reading_body(
    api_database,
    import_settings: Settings,
) -> None:
    limited = import_settings.model_copy(update={"local_import_max_bytes": 8})

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with api_database() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: limited
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/imports/preview",
            params={"filename": "playlist.txt"},
            content=b"Beyonce - Deja Vu",
        )
    app.dependency_overrides.clear()

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_size_limit"


@pytest.mark.parametrize(
    ("payload", "filename", "expected_code"),
    [
        (b"\xff\xfeA", "playlist.txt", "invalid_encoding"),
        (
            b"title,artist\n" + b"x" * 200_000 + b",Artist\n",
            "playlist.csv",
            "invalid_document",
        ),
    ],
)
async def test_preview_returns_structured_errors_for_decoder_and_csv_failures(
    api_client: httpx.AsyncClient,
    payload: bytes,
    filename: str,
    expected_code: str,
) -> None:
    response = await api_client.post(
        "/api/imports/preview",
        params={"filename": filename},
        content=payload,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code


async def test_import_routes_are_owner_scoped(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/imports/preview",
        params={"filename": "playlist.txt"},
        content=b"Beyonce - Deja Vu",
    )
    import_id = response.json()["id"]

    app.dependency_overrides[get_current_user_id] = lambda: "another-user"
    try:
        hidden = await api_client.get(f"/api/imports/{import_id}")
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)

    assert hidden.status_code == 404


async def test_queued_import_cannot_be_discarded(
    api_client: httpx.AsyncClient,
    api_database,
    import_settings: Settings,
) -> None:
    preview = (
        await api_client.post(
            "/api/imports/preview",
            params={"filename": "playlist.txt"},
            content=b"Beyonce - Deja Vu",
        )
    ).json()
    async with api_database() as session:
        await queue_import(
            session,
            import_id=preview["id"],
            user_id="local",
            job_id="job-id",
            settings=import_settings,
        )
        await session.commit()

    response = await api_client.delete(f"/api/imports/{preview['id']}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "import_queued"


async def test_spool_upload_enforces_chunked_limit_and_closes_on_failure() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"1234"
        yield b"5678"
        yield b"9"

    with pytest.raises(Exception) as exc_info:
        await spool_upload(
            chunks(),
            ImportLimits(max_upload_bytes=8, spool_memory_bytes=4),
        )

    assert getattr(exc_info.value, "code", None) == "upload_size_limit"


async def test_cleanup_reaps_expired_ready_and_failed_but_preserves_active_queue(
    api_database,
    import_settings: Settings,
) -> None:
    now = datetime.now(UTC)
    async with api_database() as session:
        session.add_all(
            [
                orm.LocalPlaylistImport(
                    id="ready",
                    user_id="local",
                    filename="ready.txt",
                    detected_format="txt",
                    file_size=1,
                    status="ready",
                    playlists=[],
                    issues=[],
                    limits={},
                    expires_at=now - timedelta(seconds=1),
                ),
                orm.LocalPlaylistImport(
                    id="failed",
                    user_id="local",
                    filename="failed.txt",
                    detected_format="txt",
                    file_size=1,
                    status="failed",
                    queued_job_id="failed-job",
                    playlists=[],
                    issues=[],
                    limits={},
                    expires_at=now - timedelta(seconds=1),
                ),
                orm.LocalPlaylistImport(
                    id="stale-queued",
                    user_id="local",
                    filename="stale.txt",
                    detected_format="txt",
                    file_size=1,
                    status="queued",
                    queued_job_id="stale-job",
                    playlists=[],
                    issues=[],
                    limits={},
                    expires_at=now - timedelta(seconds=1),
                ),
                orm.MigrationJob(
                    id="stale-job",
                    user_id="local",
                    source_provider="local_file",
                    target_provider="spotify",
                    source_account_id="stale-queued",
                    target_account_id="target",
                    selection={},
                    status="pending",
                ),
                orm.LocalPlaylistImport(
                    id="active-queued",
                    user_id="local",
                    filename="active.txt",
                    detected_format="txt",
                    file_size=1,
                    status="queued",
                    queued_job_id="active-job",
                    playlists=[],
                    issues=[],
                    limits={},
                    expires_at=now + timedelta(hours=1),
                ),
                orm.MigrationJob(
                    id="active-job",
                    user_id="local",
                    source_provider="local_file",
                    target_provider="spotify",
                    source_account_id="active-queued",
                    target_account_id="target",
                    selection={},
                    status="running",
                ),
            ]
        )
        await session.commit()

        deleted = await cleanup_expired_imports(session, now=now)
        await session.commit()
        remaining = set(
            (
                await session.execute(select(orm.LocalPlaylistImport.id))
            ).scalars()
        )
        stale_job = await session.get(orm.MigrationJob, "stale-job")

    assert deleted == 3
    assert remaining == {"active-queued"}
    assert stale_job is not None
    assert stale_job.status == "failed"
    assert "lease expired" in (stale_job.error or "").lower()


def test_import_routes_do_not_expose_user_id_query_parameter() -> None:
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    assert routes
    for route in routes:
        parameter_names = {parameter.name for parameter in route.dependant.query_params}
        assert "user_id" not in parameter_names


def test_import_preview_openapi_documents_streaming_binary_body() -> None:
    operation = app.openapi()["paths"]["/api/imports/preview"]["post"]
    schema = operation["requestBody"]["content"]["application/octet-stream"]["schema"]

    assert schema == {"type": "string", "format": "binary"}
    assert {"201", "400", "413", "422"} <= set(operation["responses"])
