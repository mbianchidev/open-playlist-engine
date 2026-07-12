from app.api import playlists
from app.api.playlists import _annotate_playlist_ref, _PlaylistMigrationSummary
from app.core.models import Playlist, PlaylistKind, PlaylistRef, Track
from app.db import models as orm
from app.providers.spotify.adapter import SPOTIFY_SAVED_TRACKS_PLAYLIST_ID


class FakePersistSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        for row in self.added:
            if isinstance(row, orm.MigrationJob) and row.id is None:
                row.id = "persisted-job"


class FakeResult:
    def __init__(self, *, rows=None, row=None) -> None:
        self.rows = rows or []
        self.row = row

    def scalars(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.row


class FakeCacheSession:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results

    async def execute(self, _stmt):
        return self.results.pop(0)


class FailingAdapter:
    async def read_playlist(self, *_args, **_kwargs):
        raise AssertionError("adapter should not be called for cached tracks")

    def iter_playlists(self, *_args, **_kwargs):
        raise AssertionError("adapter should not be called for cached playlist refs")


def test_full_migration_without_provider_track_count_is_migrated() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist")
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1", "track:2"},
        completed_full_playlist=True,
        completed_item_count=2,
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


def test_playlist_annotation_preserves_provider_note_without_migration_summary() -> None:
    ref = PlaylistRef(
        id="spotify:saved-tracks",
        name="Liked Songs",
        migration_note="Reconnect Spotify to migrate saved songs",
    )

    annotated = _annotate_playlist_ref(ref, None)

    assert annotated.migration_note == "Reconnect Spotify to migrate saved songs"


def test_review_accepted_full_migration_is_migrated_even_when_count_is_conservative() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=2)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1"},
        completed_full_playlist=True,
        completed_item_count=2,
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
        completed_item_count=2,
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


def test_completed_full_migration_with_new_source_tracks_is_delta() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=3)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1", "track:2"},
        completed_full_playlist=True,
        completed_item_count=2,
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "delta"
    assert annotated.migration_note == "Delta available: 1 new"
    assert annotated.remaining_track_count == 1


def test_later_migration_clears_prior_real_skip_for_same_source_song() -> None:
    ref = PlaylistRef(id="playlist", name="Playlist", track_count=1)
    summary = _PlaylistMigrationSummary(
        migrated_keys={"track:1"},
        skipped_keys={"track:1"},
        completed_full_playlist=True,
        completed_item_count=1,
    )

    annotated = _annotate_playlist_ref(ref, summary)

    assert annotated.migration_status == "migrated"
    assert annotated.migration_note == "Migrated"


async def test_discovered_full_migration_is_persisted(monkeypatch) -> None:
    async def no_existing_full_playlist(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(playlists, "_completed_full_playlist_exists", no_existing_full_playlist)
    session = FakePersistSession()
    playlist = Playlist(
        id="playlist",
        name="Already migrated",
        tracks=[
            Track(
                id="source-1",
                title="Song One",
                artist="Artist",
                provider_uris={"spotify": "spotify:track:source-1"},
                migration_status="migrated",
                migrated_target_playlist_id="target-playlist",
                migrated_target_uri="ytmusic:video:target-1",
            ),
            Track(
                id="source-2",
                title="Song Two",
                artist="Artist",
                migration_status="migrated",
                migrated_target_playlist_id="target-playlist",
                migrated_target_uri="ytmusic:video:target-2",
            ),
        ],
    )

    await playlists._persist_discovered_full_migration(
        session,
        user_id="local",
        source_provider="spotify",
        source_account_id="source-account",
        target_provider="ytmusic",
        target_account_id="target-account",
        playlist_id="playlist",
        playlist=playlist,
    )

    jobs = [row for row in session.added if isinstance(row, orm.MigrationJob)]
    items = [row for row in session.added if isinstance(row, orm.JobItem)]
    assert len(jobs) == 1
    assert jobs[0].status == "done"
    assert jobs[0].selection == {"playlist_ids": ["playlist"], "tracks": {}}
    assert jobs[0].total == 2
    assert jobs[0].done == 2
    assert len(items) == 2
    assert {item.status for item in items} == {"written"}
    assert {item.target_uri for item in items} == {
        "ytmusic:video:target-1",
        "ytmusic:video:target-2",
    }


async def test_cached_playlist_refs_skip_provider_call() -> None:
    session = FakeCacheSession(
        [
            FakeResult(
                rows=[
                    orm.CachedPlaylistRef(
                        user_id="local",
                        provider="spotify",
                        account_id="account",
                        playlist_id="playlist",
                        name="Cached",
                        track_count=1,
                        snapshot_id="snap-1",
                    )
                ]
            )
        ]
    )

    refs = await playlists._playlist_refs(
        session,
        adapter=FailingAdapter(),
        credential=object(),
        user_id="local",
        provider="spotify",
        account_id="account",
        refresh=False,
    )

    assert refs[0].id == "playlist"
    assert refs[0].snapshot_id == "snap-1"


async def test_cached_spotify_liked_songs_preserves_collection_kind() -> None:
    session = FakeCacheSession(
        [
            FakeResult(
                rows=[
                    orm.CachedPlaylistRef(
                        user_id="local",
                        provider="spotify",
                        account_id="account",
                        playlist_id=SPOTIFY_SAVED_TRACKS_PLAYLIST_ID,
                        name="Liked Songs",
                        track_count=1,
                    )
                ]
            )
        ]
    )

    refs = await playlists._playlist_refs(
        session,
        adapter=FailingAdapter(),
        credential=object(),
        user_id="local",
        provider="spotify",
        account_id="account",
        refresh=False,
    )

    assert refs[0].kind is PlaylistKind.LIKED_TRACKS


async def test_cached_playlist_tracks_skip_provider_call_when_snapshot_matches() -> None:
    session = FakeCacheSession(
        [
            FakeResult(
                row=orm.CachedPlaylistRef(
                    user_id="local",
                    provider="spotify",
                    account_id="account",
                    playlist_id="playlist",
                    name="Cached",
                    track_count=1,
                    snapshot_id="snap-1",
                )
            ),
            FakeResult(
                row=orm.CachedPlaylistTracks(
                    user_id="local",
                    provider="spotify",
                    account_id="account",
                    playlist_id="playlist",
                    snapshot_id="snap-1",
                    name="Cached",
                    tracks=[
                        {
                            "id": "track",
                            "title": "Song",
                            "artist": "Artist",
                            "provider_uris": {"spotify": "spotify:track:track"},
                        }
                    ],
                )
            ),
        ]
    )

    playlist = await playlists._playlist_detail(
        session,
        adapter=FailingAdapter(),
        credential=object(),
        user_id="local",
        provider="spotify",
        account_id="account",
        playlist_id="playlist",
        refresh=False,
    )

    assert playlist.snapshot_id == "snap-1"
    assert playlist.tracks[0].title == "Song"
