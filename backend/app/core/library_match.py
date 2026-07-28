"""Conservative matching for saved albums and followed/favorite artists."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.core.adapter import (
    AlbumCandidate,
    ArtistCandidate,
    FollowedArtistWriter,
    ProviderCredential,
    SavedAlbumWriter,
)
from app.core.models import Album, Artist

LibraryCandidate = AlbumCandidate | ArtistCandidate
_FEATURE_SUFFIX = re.compile(r"\s*[\(\[]?\s*(feat|ft|with)\.?\s.*$", re.IGNORECASE)
_UNICODE_NOISE = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class LibraryMatchResult:
    candidate: LibraryCandidate | None
    confidence: float
    source: str
    needs_review: bool
    review_reason: str | None = None


def _ratio(left: str | None, right: str | None) -> float:
    left_normalized = _normalize(left)
    right_normalized = _normalize(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def _artist_signature(values: list[str]) -> str:
    normalized = [_normalize(value) for value in values]
    return " ".join(sorted(value for value in normalized if value))


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _FEATURE_SUFFIX.sub("", normalized)
    return _UNICODE_NOISE.sub(" ", normalized).strip()


def score_album(album: Album, candidate: AlbumCandidate) -> tuple[float, str]:
    if album.upc and candidate.upc:
        if album.upc.upper() == candidate.upc.upper():
            return 1.0, "upc_exact"
        return 0.0, "upc_conflict"

    title = _ratio(album.title, candidate.title)
    artists = _ratio(_artist_signature(album.artists), _artist_signature(candidate.artists))
    release = 0.0
    source_year = album.release_date.year if album.release_date else album.release_year
    if source_year and candidate.release_date:
        release = 1.0 if str(source_year) == candidate.release_date[:4] else 0.0
    return round(0.6 * title + 0.35 * artists + 0.05 * release, 4), "metadata"


def score_artist(artist: Artist, candidate: ArtistCandidate) -> tuple[float, str]:
    return round(_ratio(artist.name, candidate.name), 4), "name"


class LibraryMatchService:
    def __init__(self, review_threshold: float = 0.8) -> None:
        self._threshold = review_threshold

    async def resolve_album(
        self,
        album: Album,
        target: SavedAlbumWriter,
        cred: ProviderCredential,
    ) -> LibraryMatchResult:
        direct_uri = album.provider_uris.get(target.info.name)
        if direct_uri and await target.validate_album_uri(cred, direct_uri):
            return LibraryMatchResult(
                AlbumCandidate(
                    provider_album_id=album.id or direct_uri,
                    uri=direct_uri,
                    title=album.title,
                    artists=album.artists,
                    upc=album.upc,
                    release_date=(
                        str(album.release_date)
                        if album.release_date
                        else str(album.release_year) if album.release_year else None
                    ),
                    artwork_uri=album.artwork_uri,
                ),
                1.0,
                "provider_uri",
                False,
            )

        candidates = await target.search_albums(cred, album)
        if not candidates:
            return LibraryMatchResult(None, 0.0, "none", True, "No target album match found.")
        ranked = sorted(
            ((candidate, *score_album(album, candidate)) for candidate in candidates),
            key=lambda row: row[1],
            reverse=True,
        )
        candidate, confidence, source = ranked[0]
        conflicting_upc = source == "upc_conflict"
        ambiguous = (
            len(ranked) > 1
            and source != "upc_exact"
            and abs(confidence - ranked[1][1]) <= 0.02
        )
        return LibraryMatchResult(
            candidate,
            confidence,
            source,
            confidence < self._threshold or ambiguous or conflicting_upc,
            (
                "Source and target album UPCs conflict."
                if conflicting_upc
                else "Multiple target albums are equally plausible."
                if ambiguous
                else (
                    f"Album match confidence {confidence:.2f} is below the review threshold."
                    if confidence < self._threshold
                    else None
                )
            ),
        )

    async def resolve_artist(
        self,
        artist: Artist,
        target: FollowedArtistWriter,
        cred: ProviderCredential,
    ) -> LibraryMatchResult:
        direct_uri = artist.provider_uris.get(target.info.name)
        if direct_uri and await target.validate_artist_uri(cred, direct_uri):
            return LibraryMatchResult(
                ArtistCandidate(
                    provider_artist_id=artist.id or direct_uri,
                    uri=direct_uri,
                    name=artist.name,
                    artwork_uri=artist.artwork_uri,
                ),
                1.0,
                "provider_uri",
                False,
            )

        candidates = await target.search_artists(cred, artist)
        if not candidates:
            return LibraryMatchResult(None, 0.0, "none", True, "No target artist match found.")
        ranked = sorted(
            ((candidate, *score_artist(artist, candidate)) for candidate in candidates),
            key=lambda row: row[1],
            reverse=True,
        )
        candidate, confidence, source = ranked[0]
        ambiguous = len(ranked) > 1 and abs(confidence - ranked[1][1]) <= 0.02
        reason = (
            "Multiple target artists are equally plausible."
            if ambiguous
            else "Artist names are not stable cross-provider identifiers."
        )
        return LibraryMatchResult(candidate, confidence, source, True, reason)
