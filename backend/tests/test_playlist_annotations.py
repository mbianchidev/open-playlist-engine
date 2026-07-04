from app.api.playlists import _annotate_playlist_ref, _PlaylistMigrationSummary
from app.core.models import PlaylistRef


def test_full_migration_without_provider_track_count_is_migrated() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist")
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1", "track:2"},
        completed_full_playlist=True,
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "migrated"
    assert annotated.migration_note == "Migrated"
    assert annotated.migrated_track_count == 2
    assert annotated.remaining_track_count is None


def test_unknown_total_without_full_migration_stays_partial() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist")
    summary = _PlaylistMigrationSummary(migrated_keys={"track:1"})

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "partial"
    assert annotated.migration_note == "Partially migrated"
