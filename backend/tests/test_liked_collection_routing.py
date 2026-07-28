from __future__ import annotations

import pytest

from app.api.migrations import Selection, _validate_target_capabilities
from app.core.adapter import (
    AccessDenied,
    AuthKind,
    CreatePlaylistSpec,
    ProviderCredential,
    ProviderInfo,
    Unsupported,
)
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import Playlist, PlaylistKind
from app.db import models as orm
from app.jobs.migration import _resolve_target_playlist


class LibraryTarget:
    def __init__(self, *, write_library: bool = True) -> None:
        capabilities = {Capability.ADD_TRACKS}
        if write_library:
            capabilities.add(Capability.WRITE_LIBRARY)
        self.info = ProviderInfo(
            name="target",
            display_name="Target",
            auth_kind=AuthKind.OAUTH_PKCE,
            capabilities=CapabilityDescriptor(capabilities=capabilities),
            liked_tracks_playlist_id="target:liked",
            library_read_scope="library.read",
            library_write_scope="library.write",
        )
        self.created = False

    async def create_playlist(
        self, cred: ProviderCredential, spec: CreatePlaylistSpec
    ) -> str:
        self.created = True
        return "created"


def _cred(*scopes: str) -> ProviderCredential:
    return ProviderCredential(
        account_id="account",
        provider="target",
        auth_kind=AuthKind.OAUTH_PKCE,
        scopes=list(scopes),
    )


async def test_liked_collection_routes_to_native_target_without_creating_playlist() -> None:
    target = LibraryTarget()
    target_id = await _resolve_target_playlist(
        None,
        job=orm.MigrationJob(id="job", user_id="local"),
        target=target,
        target_cred=_cred("library.read", "library.write"),
        playlist_id="source:liked",
        playlist_name="Liked Songs",
        description="",
        playlist_kind=PlaylistKind.LIKED_TRACKS,
        source_tracks=[],
    )

    assert target_id == "target:liked"
    assert target.created is False


async def test_liked_collection_never_falls_back_to_normal_playlist() -> None:
    target = LibraryTarget(write_library=False)

    with pytest.raises(Unsupported, match="cannot write liked tracks"):
        await _resolve_target_playlist(
            None,
            job=orm.MigrationJob(id="job", user_id="local"),
            target=target,
            target_cred=_cred("library.read", "library.write"),
            playlist_id="source:liked",
            playlist_name="Liked Songs",
            description="",
            playlist_kind=PlaylistKind.LIKED_TRACKS,
            source_tracks=[],
        )

    assert target.created is False


def test_preflight_requires_reconnect_for_missing_library_write_scope() -> None:
    target = LibraryTarget()

    with pytest.raises(AccessDenied, match="library.write"):
        _validate_target_capabilities(
            target,
            _cred("library.read"),
            {
                "source:liked": Playlist(
                    id="source:liked",
                    name="Liked Songs",
                    kind=PlaylistKind.LIKED_TRACKS,
                )
            },
            Selection(playlist_ids=["source:liked"]),
        )


def test_preflight_rejects_saved_albums_for_unsupported_target() -> None:
    target = LibraryTarget()

    with pytest.raises(Unsupported, match="saved albums"):
        _validate_target_capabilities(
            target,
            _cred("library.read", "library.write"),
            {},
            Selection(saved_album_ids=["album-1"]),
        )
