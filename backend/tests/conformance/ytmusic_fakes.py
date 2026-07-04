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

    def get_library_playlists(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            {
                "playlistId": playlist_id,
                "title": playlist["title"],
                "count": len(playlist["tracks"]),
            }
            for playlist_id, playlist in self.playlists.items()
        ]
        return rows[:limit]

    def get_playlist(self, playlistId: str, limit: int | None = 100) -> dict[str, Any]:
        playlist = self.playlists.get(playlistId)
        if playlist is None:
            return {"error": "playlist not found"}
        track_limit = limit if limit is not None else len(playlist["tracks"])
        tracks = [
            {
                "videoId": video_id,
                "title": title,
                "artists": [{"name": artist}],
                "album": {"name": album},
                "duration_seconds": duration,
                "isExplicit": False,
            }
            for video_id, title, artist, album, duration in (
                _VIDEO_FIXTURES.get(video_id, (video_id, video_id, "", None, None))
                for video_id in playlist["tracks"][:track_limit]
            )
        ]
        return {
            "id": playlistId,
            "title": playlist["title"],
            "description": playlist["description"],
            "tracks": tracks,
        }

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

    def search(
        self, query: str, filter: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        return [
            {
                "videoId": "yt_song_one",
                "title": "Song One",
                "artists": [{"name": "Artist One"}],
                "album": {"name": "Album One"},
                "duration_seconds": 180,
                "isExplicit": False,
            }
        ][:limit]


_VIDEO_FIXTURES: dict[str, tuple[str, str, str, str | None, int | None]] = {
    "yt_song_one": ("yt_song_one", "Song One", "Artist One", "Album One", 180),
    "aaa111": ("aaa111", "Song One", "Artist One", "Album One", 180),
    "bbb222": ("bbb222", "Song Two", "Artist Two", "Album Two", 200),
}
