"""Recorded-fixture HTTP transport for the Spotify adapter.

Routes Spotify Web API requests to JSON files captured under ``fixtures/spotify``
so the conformance suite exercises the real adapter code without any live calls.
"""

from __future__ import annotations

import json
import pathlib

import httpx

_DIR = pathlib.Path(__file__).parent / "fixtures" / "spotify"
SPOTIFY_PLAYLIST_ID = "PL_SPOTIFY_1"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text())


def _not_found(path: str) -> httpx.Response:
    return httpx.Response(404, json={"error": {"status": 404, "message": f"no fixture: {path}"}})


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path  # includes the "/v1" base path
    parts = path.strip("/").split("/")  # e.g. ["v1", "playlists", "<id>", "tracks"]

    if path.endswith("/me/playlists"):
        return httpx.Response(200, json=_load("me_playlists.json"))
    if path.endswith("/me"):
        return httpx.Response(200, json={"id": "user1"})
    if path.endswith("/search"):
        return httpx.Response(200, json=_load("search_tracks.json"))

    if len(parts) >= 3 and parts[1] == "playlists":
        playlist_id = parts[2]
        if playlist_id != SPOTIFY_PLAYLIST_ID:
            return _not_found(path)
        if path.endswith("/tracks"):
            return httpx.Response(200, json=_load("playlist_items.json"))
        return httpx.Response(200, json=_load("playlist_meta.json"))

    if len(parts) >= 3 and parts[1] == "tracks":
        track_id = parts[2]
        if track_id == "missing":
            return httpx.Response(404, json={})
        return httpx.Response(200, json=_load("track.json"))

    return _not_found(path)


def spotify_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)
