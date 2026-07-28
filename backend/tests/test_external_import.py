from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.models import Playlist, Track
from app.imports.external import (
    ImportContentError,
    SourceConnectionRequired,
    resolve_text_import,
    resolve_url_import,
)
from app.imports.http import SafeHttpResponse
from app.settings import Settings


class _PublicAdapter:
    info = SimpleNamespace(display_name="YouTube Music")

    async def read_public_playlist(self, ref) -> Playlist:
        return Playlist(
            id=ref.id,
            name="Shared playlist",
            tracks=[Track(id="song", title="Song", artist="Artist")],
        )


class _AccountOnlyAdapter:
    info = SimpleNamespace(display_name="Spotify")


class _Fetcher:
    async def fetch(self, url: str) -> SafeHttpResponse:
        assert url == "https://share.example/api/public/shares/road-trip"
        return SafeHttpResponse(
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=(
                b'{"snapshot":{"name":"Road trip","source":{"provider":"spotify"},'
                b'"tracks":[{"position":0,"title":"One","artist":"Artist","duration_s":180}]}}'
            ),
            url=url,
        )


def _settings(**overrides: Any) -> Settings:
    values = {
        "import_max_text_bytes": 10_000,
        "import_max_items": 10,
        "import_max_line_chars": 500,
        "import_max_field_chars": 100,
        "import_open_playlist_hosts": "share.example",
    }
    values.update(overrides)
    return Settings(**values)


async def test_text_import_resolves_owned_normalized_snapshot() -> None:
    result = await resolve_text_import(
        "Björk - Jóga",
        name="Favorites",
        settings=_settings(),
    )

    assert result.source_provider == "pasted_text"
    assert result.source_locator.startswith("text:")
    assert result.playlist.name == "Favorites"
    assert result.playlist.tracks[0].title == "Jóga"


async def test_text_import_rejects_empty_track_list() -> None:
    with pytest.raises(ImportContentError):
        await resolve_text_import("   \n  ", name=None, settings=_settings())


async def test_public_provider_import_does_not_require_source_account() -> None:
    result = await resolve_url_import(
        session=object(),
        user_id="user-1",
        url="https://music.youtube.com/playlist?list=PL1234567890_AbCd",
        source_account_id=None,
        settings=_settings(),
        adapter_getter=lambda provider: _PublicAdapter(),
    )

    assert result.source_provider == "ytmusic"
    assert result.playlist.id == "PL1234567890_AbCd"
    assert result.playlist.tracks[0].position == 0


async def test_account_only_provider_returns_clear_connection_action() -> None:
    with pytest.raises(SourceConnectionRequired) as excinfo:
        await resolve_url_import(
            session=object(),
            user_id="user-1",
            url="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            source_account_id=None,
            settings=_settings(),
            adapter_getter=lambda provider: _AccountOnlyAdapter(),
        )

    assert excinfo.value.provider == "spotify"
    assert "Connect Spotify" in str(excinfo.value)


async def test_open_playlist_share_uses_safe_json_fetch_and_stable_local_id() -> None:
    result = await resolve_url_import(
        session=object(),
        user_id="user-1",
        url="https://share.example/share/road-trip",
        source_account_id=None,
        settings=_settings(),
        fetcher_factory=lambda hosts, settings: _Fetcher(),
    )

    assert result.source_provider == "openplaylist"
    assert result.playlist.id.startswith("openplaylist:")
    assert result.playlist.tracks[0].duration_s == 180
