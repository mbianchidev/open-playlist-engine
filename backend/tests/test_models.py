from datetime import UTC, datetime

from app.core.models import PlaylistRef, Track


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
