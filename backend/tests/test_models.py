from app.core.models import Track


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
