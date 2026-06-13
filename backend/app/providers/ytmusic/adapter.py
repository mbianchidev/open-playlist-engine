"""YouTube Music adapter — default write path (via ``ytmusicapi``).

Unofficial: no quota but it can break and carries account-flag risk, so it is
marked EXPERIMENTAL and only registered when ``OPE_YTMUSIC_ENABLED`` is set.
It has no ISRC, so matching is text-only and leans on the evidence graph and the
review step. Header-paste auth is offered only in self-host mode.
"""

from __future__ import annotations

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
    OrderingGuarantee,
    SearchMode,
    Stability,
)
from app.core.models import Playlist, PlaylistRef, Track
from app.core.registry import register
from app.settings import get_settings


class YTMusicAuth(AuthStrategy):
    kind = AuthKind.OAUTH_DEVICE

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        if get_settings().allow_header_paste:
            return AuthChallenge(
                shape=ChallengeShape.FORM,
                instructions=(
                    "Self-host only: paste request headers from an authenticated "
                    "music.youtube.com session, or use device-code auth."
                ),
                form_schema={"headers_raw": {"type": "string", "format": "textarea"}},
            )
        # Device-code flow (Google 'TV & Limited Input' OAuth client, post-2024).
        return AuthChallenge(
            shape=ChallengeShape.DEVICE_CODE,
            user_code="TODO",
            verification_url="https://www.google.com/device",
            poll_interval_s=5,
        )

    async def complete(self, *, user_id: str, callback: dict) -> ProviderCredential:
        raise NotImplementedError("TODO: finalize ytmusicapi oauth / validate pasted headers")

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        raise NotImplementedError("TODO: refresh oauth token")

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


class YTMusicAdapter:
    info = ProviderInfo(
        name="ytmusic",
        display_name="YouTube Music",
        auth_kind=AuthKind.OAUTH_DEVICE,
        capabilities=CapabilityDescriptor(
            capabilities={
                Capability.READ_PLAYLISTS,
                Capability.READ_TRACKS,
                Capability.CREATE_PLAYLIST,
                Capability.ADD_TRACKS,
                Capability.SET_DESCRIPTION,
            },
            has_isrc=False,
            search_modes=[SearchMode.TEXT],
            official=False,
            stability=Stability.EXPERIMENTAL,
            max_add_batch=100,
            ordering=OrderingGuarantee.BEST_EFFORT,
            warning="Unofficial API — no quota but may break and carries account-flag risk.",
        ),
    )
    auth = YTMusicAuth()

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        raise NotImplementedError("TODO: ytmusic.get_library_playlists()")
        yield  # pragma: no cover

    async def iter_playlist_items(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> AsyncIterator[Track]:
        raise NotImplementedError("TODO: ytmusic.get_playlist(id)")
        yield  # pragma: no cover

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        tracks = [t async for t in self.iter_playlist_items(cred, ref)]
        return Playlist(id=ref.id, name=ref.name, tracks=tracks)

    async def search_tracks(
        self, cred: ProviderCredential, track: Track, *, limit: int = 5
    ) -> list[TrackCandidate]:
        raise NotImplementedError("TODO: ytmusic.search(query, filter='songs')")

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        raise NotImplementedError("TODO: verify videoId still exists/available")

    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        raise NotImplementedError("TODO: ytmusic.create_playlist(name, description)")

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        raise NotImplementedError("TODO: ytmusic.add_playlist_items(playlist_id, videoIds)")


def _build() -> YTMusicAdapter | None:
    if not get_settings().ytmusic_enabled:
        return None
    return register(YTMusicAdapter())


adapter = _build()
