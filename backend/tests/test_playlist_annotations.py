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
    summary = _PlaylistMigrationSummary(skipped_keys={"track:1"})

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "partial"
    assert annotated.migration_note == "Partially migrated: 1 skipped"


def test_review_accepted_full_migration_is_migrated_even_when_count_is_conservative() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=2)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1"},
        completed_full_playlist=True,
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "migrated"
    assert annotated.migration_note == "Migrated"
    assert annotated.remaining_track_count == 0


def test_completed_full_migration_with_real_skips_is_migrated() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=2)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1"},
        skipped_keys={"track:2"},
        completed_full_playlist=True,
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "migrated"
    assert annotated.migration_note == "Migrated"
    assert annotated.remaining_track_count == 0


def test_known_track_count_with_all_items_final_is_migrated() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=2)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1"},
        skipped_keys={"track:2"},
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "migrated"
    assert annotated.migration_note == "Migrated"
    assert annotated.remaining_track_count == 0


def test_migrated_subset_without_real_skips_is_not_partial() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=2)
    summary = _PlaylistMigrationSummary(migrated_keys={"track:1"})

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status is None
    assert annotated.migration_note is None
    assert annotated.migrated_track_count == 1
    assert annotated.remaining_track_count == 1


def test_later_migration_clears_prior_real_skip_for_same_source_song() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=1)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1"},
        skipped_keys={"track:1"},
        completed_full_playlist=True,
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "migrated"
    assert annotated.migration_note == "Migrated"
