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

from app.core.capabilities import CapabilityDescriptor
from app.core.models import Playlist, PlaylistRef, Track


# --------------------------------------------------------------------------- #
# Typed errors the core understands. Adapters must raise these, not leak HTTP.
# --------------------------------------------------------------------------- #
class ProviderError(Exception):
    """Base class for adapter failures."""


class AuthExpired(ProviderError):
    pass


class RefreshTokenExpired(AuthExpired):
    pass


class RateLimited(ProviderError):
    def __init__(self, retry_after_s: float | None = None, message: str = "rate limited"):
        super().__init__(message)
        self.retry_after_s = retry_after_s


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


class AddItemResult(BaseModel):
    uri: str
    ok: bool
    position: int | None = None
    error: str | None = None


class ProviderInfo(BaseModel):
    name: str
    display_name: str
    capabilities: CapabilityDescriptor
    auth_kind: AuthKind


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
