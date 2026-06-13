"""YouTube Music adapter — default write path (via ``ytmusicapi``).

Unofficial: no quota but it can break and carries account-flag risk, so it is
marked EXPERIMENTAL and only registered when ``OPE_YTMUSIC_ENABLED`` is set.
It has no ISRC, so matching is text-only and leans on the evidence graph and the
review step. Header-paste auth is offered only in self-host mode.

This PR implements the WRITE primitives (``create_playlist`` / ``add_tracks``).
``ytmusicapi`` is synchronous, so calls run in a worker thread. The client is
built through an injectable factory so the conformance suite can drive the
adapter against an in-memory fake instead of the live, unofficial API.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol

from app.core.adapter import (
    AddItemResult,
    AuthChallenge,
    AuthExpired,
    AuthKind,
    AuthStrategy,
    ChallengeShape,
    CreatePlaylistSpec,
    ProviderCredential,
    ProviderError,
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


class YTMusicClient(Protocol):
    """The narrow slice of ``ytmusicapi.YTMusic`` this adapter depends on."""

    def create_playlist(
        self,
        title: str,
        description: str,
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
        source_playlist: str | None = None,
    ) -> str | dict[str, Any]: ...

    def add_playlist_items(
        self,
        playlistId: str,
        videoIds: list[str] | None = None,
        source_playlist: str | None = None,
        duplicates: bool = False,
    ) -> str | dict[str, Any]: ...


ClientFactory = Callable[[ProviderCredential], YTMusicClient]


def _default_client_factory(cred: ProviderCredential) -> YTMusicClient:
    """Build a real ``YTMusic`` client from stored credentials.

    Not exercised in CI (the conformance suite injects a fake). ``cred.extra``
    carries the ``ytmusicapi`` auth payload (oauth token JSON or pasted headers).
    """
    from ytmusicapi import YTMusic

    auth = cred.extra.get("auth") or cred.access_token
    if not auth:
        raise AuthExpired("missing ytmusic credentials")
    return YTMusic(auth)


def _video_id(uri: str) -> str:
    """Extract a YouTube videoId from a watch URL, ``*:video:<id>`` URI, or bare id."""
    uri = uri.strip()
    if "watch?v=" in uri:
        query = urllib.parse.urlparse(uri).query
        found = urllib.parse.parse_qs(query).get("v")
        if found:
            return found[0]
    if ":" in uri and "//" not in uri:
        return uri.rsplit(":", 1)[-1]
    return uri


def _succeeded(response: str | dict[str, Any]) -> bool:
    return isinstance(response, dict) and "SUCCEEDED" in str(response.get("status", ""))


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

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        # Injecting a factory lets the conformance suite supply an in-memory client.
        self._client_factory = client_factory or _default_client_factory

    def _client(self, cred: ProviderCredential) -> YTMusicClient:
        return self._client_factory(cred)

    # READ (TODO — out of scope for this PR) -------------------------------- #
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

    # WRITE ----------------------------------------------------------------- #
    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        client = self._client(cred)
        privacy = "PUBLIC" if spec.public else "PRIVATE"
        result = await asyncio.to_thread(
            client.create_playlist, spec.name, spec.description or "", privacy
        )
        if not isinstance(result, str):
            raise ProviderError(f"ytmusic create_playlist failed: {result}")
        return result

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        client = self._client(cred)
        batch = max(1, self.info.capabilities.max_add_batch)
        uris = list(uris)
        results: list[AddItemResult] = []
        position = 0
        for start in range(0, len(uris), batch):
            chunk = uris[start : start + batch]
            video_ids = [_video_id(u) for u in chunk]
            # duplicates=True: a migration must preserve the source's repeats.
            response = await asyncio.to_thread(
                client.add_playlist_items, playlist_id, video_ids, None, True
            )
            ok = _succeeded(response)
            for uri in chunk:
                if ok:
                    results.append(AddItemResult(uri=uri, ok=True, position=position))
                    position += 1
                else:
                    results.append(
                        AddItemResult(uri=uri, ok=False, error=f"ytmusic add failed: {response}")
                    )
        return results


def _build() -> YTMusicAdapter | None:
    if not get_settings().ytmusic_enabled:
        return None
    return register(YTMusicAdapter())


adapter = _build()
