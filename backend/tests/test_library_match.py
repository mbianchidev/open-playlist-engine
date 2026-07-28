from __future__ import annotations

from app.core.adapter import AlbumCandidate, ArtistCandidate
from app.core.library_match import LibraryMatchService
from app.core.models import Album, Artist


class MatchTarget:
    class Info:
        name = "target"

    info = Info()

    def __init__(
        self,
        *,
        albums: list[AlbumCandidate] | None = None,
        artists: list[ArtistCandidate] | None = None,
        valid_uris: set[str] | None = None,
    ) -> None:
        self.albums = albums or []
        self.artists = artists or []
        self.valid_uris = valid_uris or set()

    async def search_albums(self, cred, album: Album, *, limit: int = 5) -> list[AlbumCandidate]:
        return self.albums[:limit]

    async def search_artists(
        self, cred, artist: Artist, *, limit: int = 5
    ) -> list[ArtistCandidate]:
        return self.artists[:limit]

    async def validate_album_uri(self, cred, uri: str) -> bool:
        return uri in self.valid_uris

    async def validate_artist_uri(self, cred, uri: str) -> bool:
        return uri in self.valid_uris


async def test_album_exact_upc_is_auto_accepted() -> None:
    service = LibraryMatchService(review_threshold=0.8)
    source = Album(
        title="After Hours",
        artists=["The Weeknd"],
        upc="00602508738121",
    )
    candidate = AlbumCandidate(
        provider_album_id="target-album",
        uri="target:album:target-album",
        title="After Hours",
        artists=["The Weeknd"],
        upc="00602508738121",
    )

    result = await service.resolve_album(source, MatchTarget(albums=[candidate]), None)

    assert result.candidate == candidate
    assert result.confidence == 1.0
    assert result.source == "upc_exact"
    assert result.needs_review is False


async def test_close_album_candidates_require_review() -> None:
    service = LibraryMatchService(review_threshold=0.8)
    source = Album(title="Greatest Hits", artists=["Example Band"])
    candidates = [
        AlbumCandidate(
            provider_album_id="deluxe",
            uri="target:album:deluxe",
            title="Greatest Hits",
            artists=["Example Band"],
        ),
        AlbumCandidate(
            provider_album_id="standard",
            uri="target:album:standard",
            title="Greatest Hits",
            artists=["Example Band"],
        ),
    ]

    result = await service.resolve_album(source, MatchTarget(albums=candidates), None)

    assert result.candidate == candidates[0]
    assert result.needs_review is True
    assert result.review_reason == "Multiple target albums are equally plausible."


async def test_conflicting_album_upcs_require_review() -> None:
    service = LibraryMatchService(review_threshold=0.8)
    candidate = AlbumCandidate(
        provider_album_id="target-album",
        uri="target:album:target-album",
        title="After Hours",
        artists=["The Weeknd"],
        upc="222",
        release_date="2020-03-20",
    )

    result = await service.resolve_album(
        Album(
            title="After Hours",
            artists=["The Weeknd"],
            upc="111",
            release_year=2020,
        ),
        MatchTarget(albums=[candidate]),
        None,
    )

    assert result.confidence == 0.0
    assert result.source == "upc_conflict"
    assert result.needs_review is True
    assert result.review_reason == "Source and target album UPCs conflict."


async def test_name_only_artist_match_always_requires_review() -> None:
    service = LibraryMatchService(review_threshold=0.8)
    candidate = ArtistCandidate(
        provider_artist_id="artist-2",
        uri="target:artist:artist-2",
        name="Nirvana",
    )

    result = await service.resolve_artist(
        Artist(name="Nirvana"),
        MatchTarget(artists=[candidate]),
        None,
    )

    assert result.candidate == candidate
    assert result.confidence == 1.0
    assert result.needs_review is True
    assert result.review_reason == "Artist names are not stable cross-provider identifiers."


async def test_target_provider_uri_is_stable_evidence() -> None:
    service = LibraryMatchService(review_threshold=0.8)
    uri = "target:artist:known"
    source = Artist(name="Known Artist", provider_uris={"target": uri})

    result = await service.resolve_artist(
        source,
        MatchTarget(valid_uris={uri}),
        None,
    )

    assert result.candidate is not None
    assert result.candidate.uri == uri
    assert result.confidence == 1.0
    assert result.source == "provider_uri"
    assert result.needs_review is False


async def test_non_latin_album_metadata_does_not_collapse_to_empty_match() -> None:
    service = LibraryMatchService(review_threshold=0.8)
    candidate = AlbumCandidate(
        provider_album_id="candidate",
        uri="target:album:candidate",
        title="別の作品",
        artists=["別の歌手"],
    )

    result = await service.resolve_album(
        Album(title="夜に駆ける", artists=["YOASOBI"]),
        MatchTarget(albums=[candidate]),
        None,
    )

    assert result.confidence < 0.8
    assert result.needs_review is True
