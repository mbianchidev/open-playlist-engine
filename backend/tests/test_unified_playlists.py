from app.core.models import Playlist, Track
from app.core.unified_playlists import (
    ProviderPlaylist,
    SyncLink,
    build_unified_playlists,
)


def _track(
    track_id: str,
    title: str,
    *,
    provider: str,
    position: int,
    isrc: str | None = None,
) -> Track:
    return Track(
        id=track_id,
        title=title,
        artist="Artist",
        isrc=isrc,
        position=position,
        provider_uris={provider: f"{provider}:track:{track_id}"},
    )


def _playlist(
    provider: str,
    account_id: str,
    playlist_id: str,
    name: str,
    tracks: list[Track],
) -> ProviderPlaylist:
    return ProviderPlaylist(
        provider=provider,
        account_id=account_id,
        account_label=f"{provider.title()} account",
        playlist=Playlist(id=playlist_id, name=name, tracks=tracks),
    )


def test_build_unified_playlists_merges_cross_provider_tracks_and_reports_drift() -> None:
    spotify = _playlist(
        "spotify",
        "spotify-account",
        "roadtrip",
        "Road Trip",
        [
            _track("spotify-one", "One", provider="spotify", position=0, isrc="USAAA0000001"),
            _track("spotify-two", "Two", provider="spotify", position=1, isrc="USAAA0000002"),
        ],
    )
    tidal = _playlist(
        "tidal",
        "tidal-account",
        "road-trip",
        "road trip",
        [_track("tidal-one", "One", provider="tidal", position=0, isrc="USAAA0000001")],
    )

    playlists = build_unified_playlists([spotify, tidal])

    assert len(playlists) == 1
    playlist = playlists[0]
    assert playlist.name == "Road Trip"
    assert playlist.alignment == "drifted"
    assert len(playlist.members) == 2
    assert len(playlist.tracks) == 2
    assert {source.provider for source in playlist.tracks[0].sources} == {"spotify", "tidal"}
    assert {source.provider for source in playlist.tracks[1].sources} == {"spotify"}


def test_build_unified_playlists_does_not_merge_duplicate_names_from_one_account() -> None:
    spotify_primary = _playlist(
        "spotify",
        "spotify-account",
        "roadtrip-primary",
        "Road Trip",
        [_track("spotify-one", "One", provider="spotify", position=0, isrc="USAAA0000001")],
    )
    spotify_alt = _playlist(
        "spotify",
        "spotify-account",
        "roadtrip-alt",
        "Road Trip",
        [_track("spotify-two", "Two", provider="spotify", position=0, isrc="USAAA0000002")],
    )
    tidal = _playlist(
        "tidal",
        "tidal-account",
        "roadtrip",
        "Road Trip",
        [_track("tidal-one", "One", provider="tidal", position=0, isrc="USAAA0000001")],
    )

    playlists = build_unified_playlists([spotify_primary, spotify_alt, tidal])

    assert len(playlists) == 2
    grouped_members = [
        {(member.provider, member.playlist_id) for member in playlist.members}
        for playlist in playlists
    ]
    assert {("spotify", "roadtrip-primary"), ("tidal", "roadtrip")} in grouped_members
    assert {("spotify", "roadtrip-alt")} in grouped_members


def test_explicit_sync_link_groups_renamed_copies_and_selects_the_source_as_canonical() -> None:
    source = _playlist(
        "spotify",
        "spotify-account",
        "source",
        "Morning focus",
        [_track("spotify-one", "One", provider="spotify", position=0, isrc="USAAA0000001")],
    )
    target = _playlist(
        "ytmusic",
        "youtube-account",
        "target",
        "Focus copy",
        [_track("youtube-two", "Two", provider="ytmusic", position=0, isrc="USAAA0000002")],
    )

    playlists = build_unified_playlists(
        [source, target],
        sync_links=[
            SyncLink(
                source_provider="spotify",
                source_account_id="spotify-account",
                source_playlist_id="source",
                target_provider="ytmusic",
                target_account_id="youtube-account",
                target_playlist_id="target",
                rule_id="sync-rule",
                enabled=True,
                status="idle",
            )
        ],
    )

    assert len(playlists) == 1
    playlist = playlists[0]
    assert playlist.name == "Morning focus"
    assert playlist.canonical_member_key == "spotify:spotify-account:source"
    assert playlist.alignment == "drifted"
    assert playlist.sync_rule_ids == ["sync-rule"]
    assert playlist.sync_links[0].source_member_key == "spotify:spotify-account:source"
    assert playlist.sync_links[0].target_member_key == "ytmusic:youtube-account:target"


def test_identical_ordered_copies_are_aligned() -> None:
    spotify = _playlist(
        "spotify",
        "spotify-account",
        "mix",
        "Mix",
        [
            _track("spotify-one", "One", provider="spotify", position=0, isrc="USAAA0000001"),
            _track("spotify-two", "Two", provider="spotify", position=1, isrc="USAAA0000002"),
        ],
    )
    apple = _playlist(
        "applemusic",
        "apple-account",
        "mix",
        "Mix",
        [
            _track("apple-one", "One", provider="applemusic", position=0, isrc="USAAA0000001"),
            _track("apple-two", "Two", provider="applemusic", position=1, isrc="USAAA0000002"),
        ],
    )

    playlists = build_unified_playlists([spotify, apple])

    assert playlists[0].alignment == "aligned"


def test_alignment_matches_same_song_when_only_one_provider_has_isrc() -> None:
    spotify = _playlist(
        "spotify",
        "spotify-account",
        "mix",
        "Mix",
        [_track("spotify-one", "One", provider="spotify", position=0, isrc="USAAA0000001")],
    )
    youtube = _playlist(
        "ytmusic",
        "youtube-account",
        "mix",
        "Mix",
        [_track("youtube-one", "One", provider="ytmusic", position=0)],
    )

    playlists = build_unified_playlists([spotify, youtube])

    assert playlists[0].alignment == "aligned"


def test_duplicate_song_occurrences_remain_separate_rows() -> None:
    spotify = _playlist(
        "spotify",
        "spotify-account",
        "mix",
        "Mix",
        [
            _track("spotify-one-a", "One", provider="spotify", position=0, isrc="USAAA0000001"),
            _track("spotify-one-b", "One", provider="spotify", position=1, isrc="USAAA0000001"),
        ],
    )
    tidal = _playlist(
        "tidal",
        "tidal-account",
        "mix",
        "Mix",
        [_track("tidal-one", "One", provider="tidal", position=0, isrc="USAAA0000001")],
    )

    playlists = build_unified_playlists([spotify, tidal])

    assert playlists[0].alignment == "drifted"
    assert len(playlists[0].tracks) == 2
    assert len(playlists[0].tracks[0].sources) == 2
    assert len(playlists[0].tracks[1].sources) == 1
    assert playlists[0].tracks[0].key != playlists[0].tracks[1].key


def test_empty_same_name_playlists_are_not_inferred_as_copies() -> None:
    spotify = _playlist("spotify", "spotify-account", "mix", "Mix", [])
    tidal = _playlist("tidal", "tidal-account", "mix", "Mix", [])

    playlists = build_unified_playlists([spotify, tidal])

    assert len(playlists) == 2


def test_conflicting_isrcs_do_not_merge_same_title_recordings() -> None:
    spotify = _playlist(
        "spotify",
        "spotify-account",
        "mix",
        "Mix",
        [_track("spotify-one", "One", provider="spotify", position=0, isrc="USAAA0000001")],
    )
    tidal = _playlist(
        "tidal",
        "tidal-account",
        "mix",
        "Mix",
        [_track("tidal-one", "One", provider="tidal", position=0, isrc="USAAA0000002")],
    )

    playlists = build_unified_playlists([spotify, tidal])

    assert len(playlists) == 2
