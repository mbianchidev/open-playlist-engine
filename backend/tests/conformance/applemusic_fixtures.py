"""Recorded-fixture HTTP transport for the Apple Music adapter."""

from __future__ import annotations

import json
import pathlib

import httpx

_DIR = pathlib.Path(__file__).parent / "fixtures" / "applemusic"
APPLE_MUSIC_PLAYLIST_ID = "p.ope-roadtrip"
CREATED_APPLE_MUSIC_PLAYLIST_ID = "p.ope-created"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text())


def _not_found(path: str) -> httpx.Response:
    return httpx.Response(
        404,
        json={"errors": [{"status": "404", "title": "Not Found", "detail": path}]},
    )


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/me/storefront":
        return httpx.Response(200, json=_load("storefront.json"))
    if path == "/v1/me/library/playlists" and request.method == "GET":
        fixture = "playlists_page_2.json" if request.url.params.get("offset") else "playlists.json"
        return httpx.Response(200, json=_load(fixture))
    if path == "/v1/me/library/playlists" and request.method == "POST":
        return httpx.Response(201, json=_load("create_playlist.json"))
    if path == f"/v1/me/library/playlists/{APPLE_MUSIC_PLAYLIST_ID}":
        return httpx.Response(200, json=_load("playlist_meta.json"))
    if path == "/v1/me/library/playlists/missing":
        return _not_found(path)
    if path == f"/v1/me/library/playlists/{APPLE_MUSIC_PLAYLIST_ID}/tracks":
        fixture = (
            "playlist_tracks_page_2.json"
            if request.url.params.get("offset")
            else "playlist_tracks.json"
        )
        return httpx.Response(200, json=_load(fixture))
    if (
        path == f"/v1/me/library/playlists/{CREATED_APPLE_MUSIC_PLAYLIST_ID}/tracks"
        and request.method == "POST"
    ):
        return httpx.Response(204)
    if path == "/v1/catalog/us/search":
        return httpx.Response(200, json=_load("search.json"))
    if path == "/v1/catalog/us/songs":
        if request.url.params.get("filter[isrc]"):
            return httpx.Response(200, json=_load("catalog_isrc.json"))
        return httpx.Response(200, json=_load("catalog_songs.json"))
    if path == "/v1/catalog/us/songs/missing":
        return _not_found(path)
    if path.startswith("/v1/catalog/us/songs/"):
        song_id = path.rsplit("/", 1)[-1]
        payload = _load("catalog_songs.json")
        payload["data"] = [item for item in payload["data"] if item["id"] == song_id]
        return httpx.Response(200, json=payload)
    return _not_found(path)


def applemusic_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)
