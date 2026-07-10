"""Recorded-fixture HTTP transport for the Tidal adapter."""

from __future__ import annotations

import json
import pathlib
from urllib.parse import unquote

import httpx

_DIR = pathlib.Path(__file__).parent / "fixtures" / "tidal"
TIDAL_PLAYLIST_ID = "pl_tidal_1"
CREATED_TIDAL_PLAYLIST_ID = "pl_tidal_created"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text())


def _not_found(path: str) -> httpx.Response:
    return httpx.Response(404, json={"errors": [{"detail": f"no fixture: {path}"}]})


def _tracks_document(track_ids: set[str] | None = None) -> dict:
    payload = _load("tracks.json")
    if track_ids:
        payload["data"] = [
            resource for resource in payload["data"] if resource.get("id") in track_ids
        ]
    return payload


def _track_document(track_id: str) -> dict | None:
    payload = _tracks_document({track_id})
    if not payload["data"]:
        return None
    return {
        "data": payload["data"][0],
        "included": payload.get("included", []),
        "links": {"self": f"/tracks/{track_id}"},
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v2/users/me":
        return httpx.Response(200, json=_load("user.json"))
    if path == "/v2/playlists" and request.method == "GET":
        return httpx.Response(200, json=_load("playlists.json"))
    if path == "/v2/playlists" and request.method == "POST":
        return httpx.Response(201, json=_load("create_playlist.json"))
    if path == f"/v2/playlists/{TIDAL_PLAYLIST_ID}":
        return httpx.Response(200, json=_load("playlist_meta.json"))
    if path == "/v2/playlists/missing":
        return _not_found(path)
    if path == f"/v2/playlists/{TIDAL_PLAYLIST_ID}/relationships/items":
        return httpx.Response(200, json=_load("playlist_items.json"))
    if path == f"/v2/playlists/{CREATED_TIDAL_PLAYLIST_ID}/relationships/items":
        return httpx.Response(200, json={"data": [], "links": {"self": path}})
    if path == "/v2/tracks":
        isrc = request.url.params.get("filter[isrc]")
        if isrc == "US0000000001":
            return httpx.Response(200, json=_load("search_tracks.json"))
        ids = set(request.url.params.get_list("filter[id]"))
        if "missing" in ids:
            return httpx.Response(200, json={"data": [], "links": {"self": path}})
        return httpx.Response(200, json=_tracks_document(ids or None))
    if path.startswith("/v2/tracks/"):
        payload = _track_document(path.rsplit("/", 1)[-1])
        return httpx.Response(200, json=payload) if payload is not None else _not_found(path)
    if path.startswith("/v2/searchResults/") and path.endswith("/relationships/tracks"):
        query = unquote(path.split("/searchResults/", 1)[1].split("/relationships/tracks", 1)[0])
        if "Nope" in query:
            return httpx.Response(200, json={"data": [], "links": {"self": path}})
        return httpx.Response(200, json=_load("search_relationship_tracks.json"))
    return _not_found(path)


def tidal_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)
