from collections import Counter, defaultdict

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import migrations
from app.api.dependencies import get_current_user_id
from app.db import models as orm
from app.db.base import Base
from app.main import app
from app.settings import DeploymentMode, Settings


def _job(
    job_id: str,
    *,
    source_provider: str = "spotify",
    target_provider: str = "ytmusic",
    status: str = "done",
    total: int = 0,
    selection: dict | None = None,
    user_id: str = "local",
) -> orm.MigrationJob:
    return orm.MigrationJob(
        id=job_id,
        user_id=user_id,
        source_provider=source_provider,
        target_provider=target_provider,
        source_account_id=f"{source_provider}-account",
        target_account_id=f"{target_provider}-account",
        selection=selection or {"playlist_ids": ["playlist"], "tracks": {}},
        status=status,
        total=total,
    )


def _item(
    job_id: str,
    playlist_id: str,
    *,
    name: str,
    status: str,
    position: int = 0,
) -> orm.JobItem:
    return orm.JobItem(
        id=f"{job_id}-{playlist_id}-{position}",
        job_id=job_id,
        source_playlist_id=playlist_id,
        source_playlist_name=name,
        position=position,
        title=f"Song {position}",
        artist="Artist",
        status=status,
        target_playlist_id=f"target-{playlist_id}",
    )


def test_migration_option_uses_playlist_names_not_ids() -> None:
    job = _job("job", selection={"playlist_ids": ["opaque-id"], "tracks": {}})

    option = migrations._migration_option(job, ["Road Trip"])

    assert option.label == "Road Trip"
    assert "opaque-id" not in option.label


def test_pending_migration_without_names_uses_non_id_empty_state() -> None:
    job = _job(
        "pending",
        status="pending",
        selection={"playlist_ids": ["opaque-id"], "tracks": {}},
    )

    option = migrations._migration_option(job, [])
    stats = migrations._build_migration_stats(job, [], [])

    assert option.label == "1 playlist"
    assert "opaque-id" not in option.label
    assert stats.empty is True
    assert stats.playlist_count == 1
    assert stats.message == "No track items were recorded for this migration yet."


def test_migration_routes_do_not_expose_user_id_query_parameter() -> None:
    schema = app.openapi()
    for path, path_item in schema["paths"].items():
        if not path.startswith("/api/migrations"):
            continue
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            parameter_names = {parameter["name"] for parameter in operation.get("parameters", [])}
            assert "user_id" not in parameter_names


def test_all_migration_routes_require_current_user_dependency() -> None:
    migration_routes = [
        route
        for route in migrations.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/migrations")
    ]

    assert migration_routes
    for route in migration_routes:
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        assert get_current_user_id in dependencies, f"{route.methods} {route.path}"


def test_current_user_dependency_fails_closed_in_hosted_mode() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_id(Settings(deployment_mode=DeploymentMode.HOSTED))

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "Hosted user authentication is not configured"


def test_owned_job_statement_scopes_job_by_user() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_job("alice-job", user_id="alice"))
        session.commit()

        owned = session.scalar(migrations._owned_job_stmt("alice-job", "alice"))
        hidden = session.scalar(migrations._owned_job_stmt("alice-job", "bob"))

    assert owned is not None
    assert hidden is None


def test_single_migration_stats_count_statuses_and_playlists() -> None:
    job = _job("job", total=4)
    items = [
        _item("job", "playlist-a", name="Chill", status="written", position=0),
        _item("job", "playlist-a", name="Chill", status="skipped", position=1),
        _item("job", "playlist-b", name="Focus", status="needs_review", position=0),
        _item("job", "playlist-b", name="Focus", status="failed", position=1),
    ]

    stats = migrations._build_migration_stats(job, items, ["Chill", "Focus"])

    assert stats.label == "Chill, Focus"
    assert stats.counts.total == 4
    assert stats.counts.written == 1
    assert stats.counts.skipped == 1
    assert stats.counts.needs_review == 1
    assert stats.counts.failed == 1
    assert stats.playlist_count == 2
    assert {playlist.source_playlist_name for playlist in stats.playlists} == {"Chill", "Focus"}


def test_single_migration_groups_partial_items_by_source_playlist() -> None:
    job = _job(
        "job",
        total=2,
        selection={"playlist_ids": ["playlist-a"], "tracks": {}},
    )
    processed = _item("job", "playlist-a", name="Chill", status="written", position=0)
    pending = _item("job", "playlist-a", name="Chill", status="pending", position=1)
    pending.target_playlist_id = None

    stats = migrations._build_migration_stats(job, [processed, pending], ["Chill"])

    assert stats.playlist_count == 1
    assert len(stats.playlists) == 1
    assert stats.playlists[0].target_playlist_id == "target-playlist-a"
    assert stats.playlists[0].counts.total == 2


def test_aggregate_stats_respect_source_and_target_filters() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _job(
                    "spotify-to-yt",
                    source_provider="spotify",
                    target_provider="ytmusic",
                    total=2,
                    selection={"playlist_ids": ["playlist-a"], "tracks": {}},
                ),
                _item("spotify-to-yt", "playlist-a", name="Chill", status="written", position=0),
                _item("spotify-to-yt", "playlist-a", name="Chill", status="failed", position=1),
                _job(
                    "yt-to-spotify",
                    source_provider="ytmusic",
                    target_provider="spotify",
                    total=1,
                    selection={"playlist_ids": ["playlist-b"], "tracks": {}},
                ),
                _item("yt-to-spotify", "playlist-b", name="Focus", status="skipped", position=0),
            ]
        )
        session.commit()

        conditions = migrations._migration_filter_conditions(
            user_id="local",
            source_provider="spotify",
            target_provider="ytmusic",
        )
        jobs = list(session.scalars(select(orm.MigrationJob).where(*conditions)))
        job_ids = [job.id for job in jobs]
        rows = session.execute(migrations._aggregate_item_counts_stmt(job_ids)).all()

    status_counts_by_job: dict[str, Counter[str]] = defaultdict(Counter)
    playlist_keys: set[tuple[str, str]] = set()
    for job_id, playlist_id, status, count in rows:
        status_counts_by_job[job_id][status] += int(count)
        playlist_keys.add((job_id, playlist_id))

    stats = migrations._build_aggregate_stats(
        jobs,
        status_counts_by_job,
        playlist_keys,
        source_provider="spotify",
        target_provider="ytmusic",
    )

    assert {job_id for job_id, *_ in rows} == {"spotify-to-yt"}
    assert stats.total_migrations == 1
    assert stats.total_playlists == 1
    assert stats.counts.total == 2
    assert stats.counts.written == 1
    assert stats.counts.failed == 1
    assert stats.source_provider == "spotify"
    assert stats.target_provider == "ytmusic"
