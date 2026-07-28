"""An in-memory reference adapter used to exercise the provider contract.

Every real adapter should pass ``test_adapter_contract.py`` against the same
behaviours this fake implements.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.core.adapter import (
    AddItemResult,
    AlbumCandidate,
    ArtistCandidate,
    AuthChallenge,
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    NotFound,
    ProviderCredential,
    ProviderInfo,
    TrackCandidate,
)
from app.core.capabilities import Capability, CapabilityDescriptor, SearchMode, Stability
from app.core.models import (
    Album,
    Artist,
    ArtistCollectionSemantics,
    Playlist,
    PlaylistRef,
    Track,
)


def fake_cred(provider: str) -> ProviderCredential:
    return ProviderCredential(
        account_id="acc-1", provider=provider, auth_kind=AuthKind.LONG_LIVED_TOKEN
    )


class FakeAuth:
    kind = AuthKind.LONG_LIVED_TOKEN

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        return AuthChallenge(shape=ChallengeShape.FORM, instructions="paste token")

    async def complete(self, *, user_id: str, callback: dict[str, Any]) -> ProviderCredential:
        return fake_cred("fake")

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        return cred

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


_LIBRARY: dict[str, Playlist] = {
    "pl-1": Playlist(
        id="pl-1",
        name="Roadtrip",
        tracks=[
            Track(title="Song One", artist="Artist One", isrc="US0000000001", position=0),
            Track(title="Song Two", artist="Artist Two", isrc="US0000000002", position=1),
        ],
    )
}


class FakeAdapter:
    def __init__(self) -> None:
        self.info = ProviderInfo(
            name="fake",
            display_name="Fake Provider",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={
                    Capability.READ_PLAYLISTS,
                    Capability.READ_TRACKS,
                    Capability.READ_SAVED_ALBUMS,
                    Capability.WRITE_SAVED_ALBUMS,
                    Capability.READ_FOLLOWED_ARTISTS,
                    Capability.WRITE_FOLLOWED_ARTISTS,
                    Capability.CREATE_PLAYLIST,
                    Capability.ADD_TRACKS,
                },
                has_isrc=True,
                search_modes=[SearchMode.ISRC, SearchMode.TEXT],
                stability=Stability.STABLE,
                max_add_batch=2,
                max_library_batch=2,
            ),
            artist_collection_semantics=ArtistCollectionSemantics.FOLLOW,
        )
        self.auth = FakeAuth()
        self._created: dict[str, list[str]] = {}
        self._saved_albums = {"fake:album:album-1"}
        self._followed_artists = {"fake:artist:artist-1"}

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        for pl in _LIBRARY.values():
            yield PlaylistRef(id=pl.id or "", name=pl.name, track_count=len(pl.tracks))

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        pl = _LIBRARY.get(ref.id)
        if pl is None:
            raise NotFound(ref.id)
        for t in pl.tracks:
            yield t

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        pl = _LIBRARY.get(ref.id)
        if pl is None:
            raise NotFound(ref.id)
        return pl

    async def test_connection(self, cred: ProviderCredential) -> None:
        return None

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        hits = [
            TrackCandidate(
                provider_track_id=t.id or t.title,
                uri=f"fake:track:{t.title}",
                title=t.title,
                artist=t.artist,
                isrc=t.isrc,
            )
            for pl in _LIBRARY.values()
            for t in pl.tracks
            if track.title.lower() in t.title.lower()
        ]
        return hits[:limit]

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        return uri.startswith("fake:track:")

    async def iter_saved_albums(self, cred: ProviderCredential) -> AsyncIterator[Album]:
        yield Album(
            id="album-1",
            title="Album One",
            artists=["Artist One"],
            upc="0123456789012",
            provider_uris={"fake": "fake:album:album-1"},
        )

    async def read_saved_album(self, cred: ProviderCredential, album_id: str) -> Album:
        async for album in self.iter_saved_albums(cred):
            if album.id == album_id:
                return album
        raise NotFound(album_id)

    async def iter_followed_artists(self, cred: ProviderCredential) -> AsyncIterator[Artist]:
        yield Artist(
            id="artist-1",
            name="Artist One",
            provider_uris={"fake": "fake:artist:artist-1"},
        )

    async def read_followed_artist(self, cred: ProviderCredential, artist_id: str) -> Artist:
        async for artist in self.iter_followed_artists(cred):
            if artist.id == artist_id:
                return artist
        raise NotFound(artist_id)

    async def search_albums(
        self, cred: ProviderCredential, album: Album, *, limit: int = 5
    ) -> list[AlbumCandidate]:
        return [
            AlbumCandidate(
                provider_album_id="album-1",
                uri="fake:album:album-1",
                title="Album One",
                artists=["Artist One"],
                upc="0123456789012",
            )
        ][:limit]

    async def search_artists(
        self, cred: ProviderCredential, artist: Artist, *, limit: int = 5
    ) -> list[ArtistCandidate]:
        return [
            ArtistCandidate(
                provider_artist_id="artist-1",
                uri="fake:artist:artist-1",
                name="Artist One",
            )
        ][:limit]

    async def validate_album_uri(self, cred: ProviderCredential, uri: str) -> bool:
        return uri.startswith("fake:album:")

    async def validate_artist_uri(self, cred: ProviderCredential, uri: str) -> bool:
        return uri.startswith("fake:artist:")

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        return [uri in self._saved_albums for uri in uris]

    async def contains_followed_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        return [uri in self._followed_artists for uri in uris]

    async def save_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self._saved_albums.update(uris)
        return [AddItemResult(uri=uri, ok=True, position=index) for index, uri in enumerate(uris)]

    async def follow_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self._followed_artists.update(uris)
        return [AddItemResult(uri=uri, ok=True, position=index) for index, uri in enumerate(uris)]

    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        pid = f"new-{len(self._created) + 1}"
        self._created[pid] = []
        return pid

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        if playlist_id not in self._created:
            raise NotFound(playlist_id)
        out: list[AddItemResult] = []
        for uri in uris:
            self._created[playlist_id].append(uri)
            pos = len(self._created[playlist_id]) - 1
            out.append(AddItemResult(uri=uri, ok=True, position=pos))
        return out
