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
        self.liked_tracks: list[str] = ["yt_song_one"]
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
        return {
            "id": playlistId,
            "title": playlist["title"],
            "description": playlist["description"],
            "owned": True,
            "author": {"name": "Fixture User", "id": "fixture-user"},
            "trackCount": len(playlist["tracks"]),
            "tracks": self._tracks(
                playlist["tracks"][:track_limit],
                playlist["set_video_ids"][:track_limit],
            ),
        }

    def get_liked_songs(self, limit: int | None = 100) -> dict[str, Any]:
        track_limit = limit if limit is not None else len(self.liked_tracks)
        return {
            "id": "LM",
            "title": "Liked Songs",
            "description": None,
            "trackCount": len(self.liked_tracks),
            "tracks": self._tracks(
                self.liked_tracks[:track_limit],
                [f"liked_{index}" for index in range(track_limit)],
            ),
        }

    def _tracks(
        self,
        video_ids: list[str],
        set_video_ids: list[str],
    ) -> list[dict[str, Any]]:
        return [
            {
                "videoId": video_id,
                "setVideoId": set_video_id,
                "title": title,
                "artists": [{"name": artist}],
                "album": {"name": album},
                "duration_seconds": duration,
                "isExplicit": False,
            }
            for video_id, set_video_id in zip(video_ids, set_video_ids, strict=True)
            for title, artist, album, duration in (
                [_VIDEO_FIXTURES.get(video_id, (video_id, video_id, "", None, None))[1:]]
            )
        ]

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
            "set_video_ids": [
                f"set_{video_id}_{index}" for index, video_id in enumerate(video_ids or [])
            ],
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
            set_video_id = f"set_{video_id}_{len(self.playlists[playlistId]['tracks']) - 1}"
            self.playlists[playlistId]["set_video_ids"].append(set_video_id)
            results.append({"videoId": video_id, "setVideoId": set_video_id})
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": results}

    def delete_playlist(self, playlistId: str) -> dict[str, Any]:
        if playlistId not in self.playlists:
            return {"status": "STATUS_FAILED", "error": "playlist not found"}
        del self.playlists[playlistId]
        return {"status": "STATUS_SUCCEEDED"}

    def remove_playlist_items(
        self,
        playlistId: str,
        videos: list[dict[str, str]],
    ) -> dict[str, Any]:
        playlist = self.playlists.get(playlistId)
        if playlist is None:
            return {"status": "STATUS_FAILED", "error": "playlist not found"}
        wanted = {video["setVideoId"] for video in videos}
        kept = [
            (video_id, set_video_id)
            for video_id, set_video_id in zip(
                playlist["tracks"],
                playlist["set_video_ids"],
                strict=True,
            )
            if set_video_id not in wanted
        ]
        playlist["tracks"] = [video_id for video_id, _ in kept]
        playlist["set_video_ids"] = [set_video_id for _, set_video_id in kept]
        return {"status": "STATUS_SUCCEEDED"}

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

    def rate_song(self, videoId: str, rating: str = "INDIFFERENT") -> dict[str, Any]:
        if rating == "LIKE" and videoId not in self.liked_tracks:
            self.liked_tracks.append(videoId)
        elif rating == "INDIFFERENT" and videoId in self.liked_tracks:
            self.liked_tracks.remove(videoId)
        return {"status": "STATUS_SUCCEEDED"}


_VIDEO_FIXTURES: dict[str, tuple[str, str, str, str | None, int | None]] = {
    "yt_song_one": ("yt_song_one", "Song One", "Artist One", "Album One", 180),
    "aaa111": ("aaa111", "Song One", "Artist One", "Album One", 180),
    "bbb222": ("bbb222", "Song Two", "Artist Two", "Album Two", 200),
}
