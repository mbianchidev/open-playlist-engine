from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import migrations
from app.db import models as orm
from app.db.base import Base


def _job(
    job_id: str,
    *,
    source_provider: str = "spotify",
    target_provider: str = "ytmusic",
    status: str = "done",
    total: int = 0,
    selection: dict | None = None,
) -> orm.MigrationJob:
    return orm.MigrationJob(
        id=job_id,
        user_id="local",
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
        items = list(
            session.scalars(
                select(orm.JobItem)
                .join(orm.MigrationJob, orm.MigrationJob.id == orm.JobItem.job_id)
                .where(*conditions)
            )
        )

    stats = migrations._build_aggregate_stats(
        jobs,
        items,
        source_provider="spotify",
        target_provider="ytmusic",
    )

    assert stats.total_migrations == 1
    assert stats.total_playlists == 1
    assert stats.counts.total == 2
    assert stats.counts.written == 1
    assert stats.counts.failed == 1
    assert stats.source_provider == "spotify"
    assert stats.target_provider == "ytmusic"
