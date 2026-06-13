"""Spotify adapter — read + write, ISRC-rich, clean OAuth (PKCE).

Network calls are TODO; the capability descriptor and auth shape are real so the
UI, registry and conformance suite work end-to-end against the contract.
"""

from __future__ import annotations

import urllib.parse
import uuid
from collections.abc import AsyncIterator, Sequence

from app.core.adapter import (
    AddItemResult,
    AuthChallenge,
    AuthKind,
    AuthStrategy,
    ChallengeShape,
    CreatePlaylistSpec,
    ProviderCredential,
    ProviderInfo,
    TrackCandidate,
)
from app.core.capabilities import (
    Capability,
    CapabilityDescriptor,
    SearchMode,
    Stability,
)
from app.core.models import Playlist, PlaylistRef, Track
from app.core.registry import register
from app.settings import get_settings

_SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]


class SpotifyAuth(AuthStrategy):
    kind = AuthKind.OAUTH_PKCE

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        s = get_settings()
        state = uuid.uuid4().hex
        params = {
            "client_id": s.spotify_client_id,
            "response_type": "code",
            "redirect_uri": s.spotify_redirect_uri,
            "scope": " ".join(_SCOPES),
            "state": state,
            # TODO: attach PKCE code_challenge and persist the verifier for `state`.
        }
        url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)
        return AuthChallenge(shape=ChallengeShape.REDIRECT, redirect_url=url, state=state)

    async def complete(self, *, user_id: str, callback: dict) -> ProviderCredential:
        raise NotImplementedError("TODO: exchange auth code (+PKCE verifier) for tokens")

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        raise NotImplementedError("TODO: refresh access token")

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


class SpotifyAdapter:
    info = ProviderInfo(
        name="spotify",
        display_name="Spotify",
        auth_kind=AuthKind.OAUTH_PKCE,
        capabilities=CapabilityDescriptor(
            capabilities={
                Capability.READ_PLAYLISTS,
                Capability.READ_TRACKS,
                Capability.READ_LIBRARY,
                Capability.CREATE_PLAYLIST,
                Capability.ADD_TRACKS,
                Capability.SET_DESCRIPTION,
                Capability.SET_COVER,
            },
            has_isrc=True,
            search_modes=[SearchMode.ISRC, SearchMode.TEXT],
            official=True,
            stability=Stability.STABLE,
            max_add_batch=100,
            max_playlist_size=10_000,
        ),
    )
    auth = SpotifyAuth()

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        raise NotImplementedError("TODO: GET /me/playlists (paginated)")
        yield  # pragma: no cover

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        raise NotImplementedError("TODO: GET /playlists/{id}/tracks (paginated, capture ISRC)")
        yield  # pragma: no cover

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        tracks = [t async for t in self.iter_playlist_items(cred, ref)]
        return Playlist(id=ref.id, name=ref.name, tracks=tracks)

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        raise NotImplementedError("TODO: GET /search (prefer isrc: query when available)")

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        raise NotImplementedError("TODO: GET /tracks/{id}")

    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        raise NotImplementedError("TODO: POST /users/{id}/playlists")

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        raise NotImplementedError("TODO: POST /playlists/{id}/tracks (batch <=100)")


adapter = register(SpotifyAdapter())
