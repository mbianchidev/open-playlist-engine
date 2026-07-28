"""The provider plugin contract.

A provider implements :class:`ProviderAdapter`. Per the duck review, adapters do
NOT touch the match graph — they only expose read/search/write primitives, and
the core :mod:`app.core.match_service` owns caching, scoring and promotion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import (
    Album,
    Artist,
    ArtistCollectionSemantics,
    Playlist,
    PlaylistRef,
    Track,
)


# --------------------------------------------------------------------------- #
# Typed errors the core understands. Adapters must raise these, not leak HTTP.
# --------------------------------------------------------------------------- #
class ProviderError(Exception):
    """Base class for adapter failures."""


class AuthExpired(ProviderError):
    pass


class RefreshTokenExpired(AuthExpired):
    pass


class AccessDenied(ProviderError):
    pass


class RateLimited(ProviderError):
    def __init__(
        self,
        retry_after_s: float | None = None,
        message: str = "rate limited",
        status_code: int = 429,
    ):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.status_code = status_code


class NotFound(ProviderError):
    pass


class Unsupported(ProviderError):
    pass


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class AuthKind(StrEnum):
    OAUTH_PKCE = "oauth_pkce"
    OAUTH_DEVICE = "oauth_device"
    HEADER_PASTE = "header_paste"
    DEVELOPER_USER_TOKEN = "developer_user_token"  # e.g. Apple MusicKit
    LONG_LIVED_TOKEN = "long_lived_token"


class ChallengeShape(StrEnum):
    """The three UI shapes every auth flow collapses into."""

    REDIRECT = "redirect"
    DEVICE_CODE = "device_code"
    FORM = "form"


class AuthChallenge(BaseModel):
    shape: ChallengeShape
    # redirect
    redirect_url: str | None = None
    state: str | None = None
    # device_code
    user_code: str | None = None
    verification_url: str | None = None
    poll_interval_s: int | None = None
    # form
    instructions: str | None = None
    form_schema: dict[str, Any] | None = None


class ProviderCredential(BaseModel):
    """Decrypted, in-memory credential. Persisted encrypted (see db.models)."""

    account_id: str
    provider: str
    auth_kind: AuthKind
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None
    scopes: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    version: int = 1


class AuthStrategy(Protocol):
    kind: AuthKind

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge: ...

    async def complete(self, *, user_id: str, callback: dict[str, Any]) -> ProviderCredential: ...

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential: ...

    async def revoke(self, cred: ProviderCredential) -> None: ...


# --------------------------------------------------------------------------- #
# Write primitives
# --------------------------------------------------------------------------- #
class CreatePlaylistSpec(BaseModel):
    name: str
    description: str | None = None
    public: bool = False


class TrackCandidate(BaseModel):
    """A possible target match returned by ``search_tracks`` — core scores these."""

    provider_track_id: str
    uri: str
    title: str
    artist: str
    album: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    explicit: bool | None = None
    market: str | None = None


class AlbumCandidate(BaseModel):
    provider_album_id: str
    uri: str
    title: str
    artists: list[str] = Field(default_factory=list)
    upc: str | None = None
    release_date: str | None = None
    artwork_uri: str | None = None


class ArtistCandidate(BaseModel):
    provider_artist_id: str
    uri: str
    name: str
    artwork_uri: str | None = None


class AddItemResult(BaseModel):
    uri: str
    ok: bool
    already_present: bool = False
    position: int | None = None
    error: str | None = None


class ProviderInfo(BaseModel):
    name: str
    display_name: str
    capabilities: CapabilityDescriptor
    auth_kind: AuthKind
    liked_tracks_playlist_id: str | None = None
    library_read_scope: str | None = None
    library_write_scope: str | None = None
    saved_albums_read_scope: str | None = None
    saved_albums_write_scope: str | None = None
    followed_artists_read_scope: str | None = None
    followed_artists_write_scope: str | None = None
    artist_collection_semantics: ArtistCollectionSemantics | None = None

    def require_liked_tracks_target(self, cred: ProviderCredential) -> str:
        if not self.capabilities.can(Capability.WRITE_LIBRARY):
            raise Unsupported(f"{self.display_name} cannot write liked tracks")
        if not self.liked_tracks_playlist_id:
            raise Unsupported(f"{self.display_name} does not expose a liked-tracks collection")
        missing_scopes = [
            scope
            for scope in (self.library_read_scope, self.library_write_scope)
            if scope and scope not in cred.scopes
        ]
        if missing_scopes:
            scopes = ", ".join(missing_scopes)
            raise AccessDenied(
                f"Reconnect {self.display_name} to grant the required library scopes: {scopes}"
            )
        return self.liked_tracks_playlist_id

    def require_saved_albums_source(self, cred: ProviderCredential) -> None:
        self._require_library_capability(
            cred,
            Capability.READ_SAVED_ALBUMS,
            self.saved_albums_read_scope,
            "read saved albums",
        )

    def require_saved_albums_target(self, cred: ProviderCredential) -> None:
        self._require_library_capability(
            cred,
            Capability.WRITE_SAVED_ALBUMS,
            self.saved_albums_write_scope,
            "write saved albums",
        )
        self._require_scope(cred, self.saved_albums_read_scope, "check saved albums")

    def require_followed_artists_source(self, cred: ProviderCredential) -> None:
        self._require_library_capability(
            cred,
            Capability.READ_FOLLOWED_ARTISTS,
            self.followed_artists_read_scope,
            "read followed or favorite artists",
        )
        if self.artist_collection_semantics is None:
            raise Unsupported(f"{self.display_name} does not define artist library semantics")

    def require_followed_artists_target(self, cred: ProviderCredential) -> None:
        self._require_library_capability(
            cred,
            Capability.WRITE_FOLLOWED_ARTISTS,
            self.followed_artists_write_scope,
            "write followed or favorite artists",
        )
        self._require_scope(
            cred,
            self.followed_artists_read_scope,
            "check followed or favorite artists",
        )
        if self.artist_collection_semantics is None:
            raise Unsupported(f"{self.display_name} does not define artist library semantics")

    def _require_library_capability(
        self,
        cred: ProviderCredential,
        capability: Capability,
        scope: str | None,
        action: str,
    ) -> None:
        if not self.capabilities.can(capability):
            raise Unsupported(f"{self.display_name} cannot {action}")
        self._require_scope(cred, scope, action)

    def _require_scope(
        self, cred: ProviderCredential, scope: str | None, action: str
    ) -> None:
        if scope and scope not in cred.scopes:
            raise AccessDenied(
                f"Reconnect {self.display_name} to grant the required scope before "
                f"{action}: {scope}"
            )


@runtime_checkable
class ProviderAdapter(Protocol):
    info: ProviderInfo
    auth: AuthStrategy

    # READ
    def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]: ...

    def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]: ...

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist: ...

    async def test_connection(self, cred: ProviderCredential) -> None: ...

    # SEARCH (used by MatchService; never writes to the graph itself)
    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]: ...

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool: ...

    # WRITE (idempotency handled by the core operation ledger)
    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str: ...

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]: ...


@runtime_checkable
class SavedAlbumReader(Protocol):
    info: ProviderInfo

    def iter_saved_albums(self, cred: ProviderCredential) -> AsyncIterator[Album]: ...

    async def read_saved_album(self, cred: ProviderCredential, album_id: str) -> Album: ...

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]: ...


@runtime_checkable
class SavedAlbumWriter(Protocol):
    info: ProviderInfo

    async def search_albums(
        self, cred: ProviderCredential, album: Album, *, limit: int = 5
    ) -> list[AlbumCandidate]: ...

    async def validate_album_uri(self, cred: ProviderCredential, uri: str) -> bool: ...

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]: ...

    async def save_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]: ...


@runtime_checkable
class FollowedArtistReader(Protocol):
    info: ProviderInfo

    def iter_followed_artists(self, cred: ProviderCredential) -> AsyncIterator[Artist]: ...

    async def read_followed_artist(self, cred: ProviderCredential, artist_id: str) -> Artist: ...

    async def contains_followed_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]: ...


@runtime_checkable
class FollowedArtistWriter(Protocol):
    info: ProviderInfo

    async def search_artists(
        self, cred: ProviderCredential, artist: Artist, *, limit: int = 5
    ) -> list[ArtistCandidate]: ...

    async def validate_artist_uri(self, cred: ProviderCredential, uri: str) -> bool: ...

    async def contains_followed_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]: ...

    async def follow_artists(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]: ...
