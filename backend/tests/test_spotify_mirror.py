import json

import httpx
import pytest

from app.core.adapter import MirrorProviderAdapter, ProviderCredential, ProviderError
from app.core.capabilities import Capability
from app.providers.spotify.adapter import SpotifyAdapter


def _credential() -> ProviderCredential:
    return ProviderCredential(
        account_id="spotify-account",
        provider="spotify",
        auth_kind="oauth_pkce",
        access_token="token",
    )


async def test_spotify_mirror_replaces_then_appends_in_ordered_chunks() -> None:
    calls: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.method, payload["uris"]))
        return httpx.Response(201, json={"snapshot_id": "snapshot"})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    uris = [f"spotify:track:{index:03d}" for index in range(205)]

    await adapter.replace_playlist_tracks(_credential(), "playlist", uris)

    assert [method for method, _ in calls] == ["PUT", "POST", "POST"]
    assert [len(chunk) for _, chunk in calls] == [100, 100, 5]
    assert [uri for _, chunk in calls for uri in chunk] == uris
    assert isinstance(adapter, MirrorProviderAdapter)
    assert adapter.info.capabilities.can(Capability.REMOVE_TRACKS)
    assert adapter.info.capabilities.can(Capability.REORDER)


async def test_spotify_mirror_retry_restarts_with_put_after_partial_failure() -> None:
    methods: list[str] = []
    failed_once = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed_once
        methods.append(request.method)
        if request.method == "POST" and not failed_once:
            failed_once = True
            return httpx.Response(500, json={"error": {"message": "temporary failure"}})
        return httpx.Response(201, json={"snapshot_id": "snapshot"})

    adapter = SpotifyAdapter(transport=httpx.MockTransport(handler))
    uris = [f"spotify:track:{index:03d}" for index in range(150)]

    with pytest.raises(ProviderError):
        await adapter.replace_playlist_tracks(_credential(), "playlist", uris)
    await adapter.replace_playlist_tracks(_credential(), "playlist", uris)

    assert methods == ["PUT", "POST", "PUT", "POST"]
