"""In-memory fake of the ``ytmusicapi.YTMusic`` write surface.

The unofficial YouTube Music API cannot be recorded as stable HTTP fixtures, so
the seam is the client object instead: the adapter takes a ``client_factory`` and
the conformance suite injects this fake, which mimics ``create_playlist`` and
``add_playlist_items`` return shapes.
"""

from __future__ import annotations

from typing import Any


class FakeYTMusic:
    def __init__(self) -> None:
        self.playlists: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def create_playlist(
        self,
        title: str,
        description: str,
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
        source_playlist: str | None = None,
    ) -> str:
        self._counter += 1
        playlist_id = f"PL_YT_{self._counter}"
        self.playlists[playlist_id] = {
            "title": title,
            "description": description,
            "privacy": privacy_status,
            "tracks": list(video_ids or []),
        }
        return playlist_id

    def add_playlist_items(
        self,
        playlistId: str,
        videoIds: list[str] | None = None,
        source_playlist: str | None = None,
        duplicates: bool = False,
    ) -> dict[str, Any]:
        if playlistId not in self.playlists:
            return {"status": "STATUS_FAILED", "error": "playlist not found"}
        results = []
        for video_id in videoIds or []:
            self.playlists[playlistId]["tracks"].append(video_id)
            results.append({"videoId": video_id, "setVideoId": f"set_{video_id}"})
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": results}
