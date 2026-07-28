from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from app.api.library import _build_library_view
from app.core.adapter import (
    AddItemResult,
    AlbumCandidate,
    ArtistCandidate,
    AuthKind,
    ProviderCredential,
    ProviderInfo,
)
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import Album, Artist, ArtistCollectionSemantics


class LibraryAdapterFake:
    auth = object()

    def __init__(
        self,
        *,
        read_albums: bool = True,
        write_albums: bool = True,
        read_artists: bool = True,
        write_artists: bool = True,
        semantics: ArtistCollectionSemantics = ArtistCollectionSemantics.FOLLOW,
        required_scope: str | None = None,
    ) -> None:
        capabilities = set()
        for enabled, capability in (
            (read_albums, Capability.READ_SAVED_ALBUMS),
            (write_albums, Capability.WRITE_SAVED_ALBUMS),
            (read_artists, Capability.READ_FOLLOWED_ARTISTS),
            (write_artists, Capability.WRITE_FOLLOWED_ARTISTS),
        ):
            if enabled:
                capabilities.add(capability)
        self.info = ProviderInfo(
            name="fake",
            display_name="Fake",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(capabilities=capabilities),
            saved_albums_read_scope=required_scope,
            saved_albums_write_scope=required_scope,
            followed_artists_read_scope=required_scope,
            followed_artists_write_scope=required_scope,
            artist_collection_semantics=semantics,
        )

    async def iter_saved_albums(self, cred: ProviderCredential) -> AsyncIterator[Album]:
        yield Album(id="album-1", title="Album One", artists=["Artist One"])

    async def read_saved_album(self, cred: ProviderCredential, album_id: str) -> Album:
        return Album(id=album_id, title="Album One", artists=["Artist One"])

    async def iter_followed_artists(self, cred: ProviderCredential) -> AsyncIterator[Artist]:
        yield Artist(id="artist-1", name="Artist One")

    async def read_followed_artist(self, cred: ProviderCredential, artist_id: str) -> Artist:
        return Artist(id=artist_id, name="Artist One")

    async def search_albums(
        self, cred: ProviderCredential, album: Album, *, limit: int = 5
    ) -> list[AlbumCandidate]:
        return []

    async def search_artists(
        self, cred: ProviderCredential, artist: Artist, *, limit: int = 5
    ) -> list[ArtistCandidate]:
        return []

    async def validate_album_uri(self, cred: ProviderCredential, uri: str) -> bool:
        return True

    async def validate_artist_uri(self, cred: ProviderCredential, uri: str) -> bool:
        return True

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        return [False] * len(uris)

    async def contains_followed_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        return [False] * len(uris)

    async def save_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        return []

    async def follow_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        return []


def _cred(*scopes: str) -> ProviderCredential:
    return ProviderCredential(
        account_id="account",
        provider="fake",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
        scopes=list(scopes),
    )


async def test_library_view_reports_items_counts_and_partial_target_support() -> None:
    source = LibraryAdapterFake()
    target = LibraryAdapterFake(write_artists=False)

    view = await _build_library_view(source, _cred(), target, _cred())

    assert view.saved_albums.count == 1
    assert view.saved_albums.items[0].title == "Album One"
    assert view.saved_albums.source_supported is True
    assert view.saved_albums.target_supported is True
    assert view.followed_artists.count == 1
    assert view.followed_artists.source_semantics == "follow"
    assert view.followed_artists.target_supported is False
    assert "cannot write" in (view.followed_artists.target_limitation or "")


async def test_library_view_surfaces_missing_scope_without_hiding_other_capabilities() -> None:
    source = LibraryAdapterFake(required_scope="library.scope")
    target = LibraryAdapterFake()

    view = await _build_library_view(source, _cred(), target, _cred())

    assert view.saved_albums.items == []
    assert view.followed_artists.items == []
    assert "library.scope" in (view.saved_albums.source_limitation or "")
    assert view.saved_albums.target_supported is True
