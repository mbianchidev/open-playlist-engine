"""An in-memory reference adapter used to exercise the provider contract.

Every real adapter should pass ``test_adapter_contract.py`` against the same
behaviours this fake implements.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.core.adapter import (
    AddItemResult,
    AuthChallenge,
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    NotFound,
    PlaylistMutationResult,
    ProviderCredential,
    ProviderInfo,
    RemoveItemResult,
    RemoveTracksResult,
    TrackCandidate,
    TrackRemoval,
)
from app.core.capabilities import Capability, CapabilityDescriptor, SearchMode, Stability
from app.core.models import Playlist, PlaylistRef, Track


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
                    Capability.CREATE_PLAYLIST,
                    Capability.ADD_TRACKS,
                },
                has_isrc=True,
                search_modes=[SearchMode.ISRC, SearchMode.TEXT],
                stability=Stability.STABLE,
                max_add_batch=2,
            ),
        )
        self.auth = FakeAuth()
        self._created: dict[str, list[str]] = {}

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

    async def unfollow_playlist(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> PlaylistMutationResult:
        _LIBRARY.pop(ref.id, None)
        return PlaylistMutationResult()

    async def delete_playlist(
        self, cred: ProviderCredential, ref: PlaylistRef
    ) -> PlaylistMutationResult:
        _LIBRARY.pop(ref.id, None)
        return PlaylistMutationResult()

    async def remove_tracks(
        self,
        cred: ProviderCredential,
        ref: PlaylistRef,
        items: Sequence[TrackRemoval],
    ) -> RemoveTracksResult:
        return RemoveTracksResult(
            items=[
                RemoveItemResult(
                    source_item_id=item.source_item_id,
                    provider_uri=item.provider_uri,
                    position=item.position,
                    ok=True,
                )
                for item in items
            ]
        )
