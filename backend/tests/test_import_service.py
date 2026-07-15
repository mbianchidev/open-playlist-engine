from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.models import Playlist, Track
from app.imports.http import SafeHttpResponse
from app.imports.service import ImportService, SourceConnectionRequired
from app.settings import Settings


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        for index, row in enumerate(self.added, start=1):
            if not row.id:
                row.id = f"import-{index}"


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
        assert url == "https://share.example/open-playlists/road-trip"
        return SafeHttpResponse(
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=(
                b'{"id":"remote","name":"Road trip","tracks":['
                b'{"id":"one","title":"One","artist":"Artist","duration":180}]}'
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


async def test_text_preview_persists_owned_normalized_snapshot() -> None:
    session = _Session()
    service = ImportService(settings=_settings())

    row = await service.preview_text(
        session,
        user_id="user-1",
        text="Björk - Jóga",
        name="Favorites",
    )

    assert row.id == "import-1"
    assert row.user_id == "user-1"
    assert row.source_provider == "text"
    assert row.source_locator.startswith("text:")
    assert row.playlist["name"] == "Favorites"
    assert row.playlist["tracks"][0]["title"] == "Jóga"


async def test_public_provider_preview_does_not_require_source_account() -> None:
    session = _Session()
    service = ImportService(
        settings=_settings(),
        adapter_getter=lambda provider: _PublicAdapter(),
    )

    row = await service.preview_url(
        session,
        user_id="user-1",
        url="https://music.youtube.com/playlist?list=PL1234567890_AbCd",
        source_account_id=None,
    )

    assert row.source_provider == "ytmusic"
    assert row.playlist_id == "PL1234567890_AbCd"
    assert row.playlist["tracks"][0]["position"] == 0


async def test_account_only_provider_returns_clear_connection_action() -> None:
    service = ImportService(
        settings=_settings(),
        adapter_getter=lambda provider: _AccountOnlyAdapter(),
    )

    with pytest.raises(SourceConnectionRequired) as excinfo:
        await service.preview_url(
            _Session(),
            user_id="user-1",
            url="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            source_account_id=None,
        )

    assert excinfo.value.provider == "spotify"
    assert "Connect Spotify" in str(excinfo.value)


async def test_open_playlist_share_uses_safe_json_fetch_and_stable_local_id() -> None:
    session = _Session()
    service = ImportService(
        settings=_settings(),
        fetcher_factory=lambda hosts: _Fetcher(),
    )

    row = await service.preview_url(
        session,
        user_id="user-1",
        url="https://share.example/share/road-trip",
        source_account_id=None,
    )

    assert row.source_provider == "openplaylist"
    assert row.playlist_id.startswith("openplaylist:")
    assert row.playlist["tracks"][0]["duration_s"] == 180
    assert row.playlist["tracks"][0]["source_item_id"] == "one"

