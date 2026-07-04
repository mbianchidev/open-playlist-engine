from app.core.migration_state import filter_unmigrated_tracks, track_keys
from app.core.models import Playlist, Track


class DeltaSource:
    def __init__(self, tracks: list[Track]) -> None:
        self._playlist = Playlist(id="pl-1", name="Roadtrip", tracks=tracks)

    async def read_playlist(self, cred, ref):
        return self._playlist


def test_filter_unmigrated_tracks_keeps_only_delta_tracks() -> None:
    migrated = Track(title="Song One", artist="Artist One", isrc="US0000000001")
    delta = Track(title="Song Two", artist="Artist Two", isrc="US0000000002")

    assert filter_unmigrated_tracks([migrated, delta], track_keys(migrated)) == [delta]


def test_filter_unmigrated_tracks_matches_provider_uri_keys() -> None:
    migrated = Track(
        title="Song One",
        artist="Artist One",
        provider_uris={"spotify": "spotify:track:known"},
    )
    delta = Track(title="Song Two", artist="Artist Two")

    assert filter_unmigrated_tracks([migrated, delta], {"uri:spotify:track:known"}) == [delta]


async def test_selected_playlists_filters_delta_tracks(monkeypatch) -> None:
    from app.api import migrations

    migrated = Track(title="Song One", artist="Artist One", isrc="US0000000001")
    delta = Track(title="Song Two", artist="Artist Two", isrc="US0000000002")

    async def fake_migrated_track_keys(*args, **kwargs) -> set[str]:
        return track_keys(migrated)

    monkeypatch.setattr(migrations, "migrated_track_keys", fake_migrated_track_keys)
    body = migrations.CreateMigration(
        source_provider="spotify",
        target_provider="ytmusic",
        source_account_id="source-account",
        target_account_id="target-account",
        selection=migrations.Selection(
            playlist_ids=["pl-1"],
            delta_playlist_ids=["pl-1"],
        ),
    )

    selected = await migrations._selected_playlists(
        object(),
        DeltaSource([migrated, delta]),
        object(),
        body,
        user_id="local",
    )

    assert selected["pl-1"].tracks == [delta]
