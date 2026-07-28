from datetime import UTC, date, datetime

from app.core.models import Album, Artist, ArtistCollectionSemantics, PlaylistRef, Track


def test_track_accepts_structured_credits() -> None:
    track = Track(
        title="Blinding Lights",
        artist="The Weeknd",
        credits=[
            {"role": "featured_artist", "name": "Daft Punk"},
            {
                "role": "producer",
                "name": "Max Martin",
                "uri": "https://music.example.com/artist/max-martin",
            },
        ],
    )

    assert track.credits[0].role == "featured_artist"
    assert track.credits[1].name == "Max Martin"
    assert track.model_dump()["credits"][1]["uri"] == "https://music.example.com/artist/max-martin"


def test_playlist_ref_accepts_organizer_metadata() -> None:
    updated_at = datetime(2026, 7, 14, tzinfo=UTC)

    playlist = PlaylistRef(
        id="playlist",
        name="Roadtrip",
        owner_id="owner",
        owner_name="Owner",
        is_owned=False,
        is_followed=True,
        updated_at=updated_at,
    )

    assert playlist.owner_name == "Owner"
    assert playlist.is_owned is False
    assert playlist.is_followed is True
    assert playlist.updated_at == updated_at


def test_album_preserves_cross_provider_matching_metadata() -> None:
    album = Album(
        id="album-1",
        title="After Hours",
        artists=["The Weeknd"],
        upc="00602508738121",
        release_date=date(2020, 3, 20),
        artwork_uri="https://images.example.com/after-hours.jpg",
        provider_uris={"spotify": "spotify:album:album-1"},
        source_item_id="saved-album-1",
    )

    assert album.artists == ["The Weeknd"]
    assert album.upc == "00602508738121"
    assert album.model_dump(mode="json")["release_date"] == "2020-03-20"
    assert album.provider_uris["spotify"] == "spotify:album:album-1"


def test_artist_collection_semantics_are_explicit() -> None:
    artist = Artist(
        id="artist-1",
        name="The Weeknd",
        provider_uris={"tidal": "tidal:artist:artist-1"},
    )

    assert artist.name == "The Weeknd"
    assert ArtistCollectionSemantics.FOLLOW.value == "follow"
    assert ArtistCollectionSemantics.FAVORITE.value == "favorite"
