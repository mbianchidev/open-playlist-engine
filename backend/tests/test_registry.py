from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.registry import all_info


def test_spotify_registered() -> None:
    names = {i.name for i in all_info()}
    assert "spotify" in names
    assert "tidal" in names
    assert "applemusic" in names


def test_providers_endpoint_capability_matrix(client: TestClient) -> None:
    r = client.get("/api/providers")
    assert r.status_code == 200
    rows = {row["name"]: row for row in r.json()}
    assert "spotify" in rows
    assert "tidal" in rows
    # Spotify can be a source (reads tracks) and has ISRC.
    assert rows["spotify"]["can_source"] is True
    assert rows["spotify"]["can_target"] is True
    assert rows["spotify"]["has_isrc"] is True
    assert rows["spotify"]["can_unfollow_playlist"] is True
    assert rows["spotify"]["can_delete_playlist"] is False
    assert rows["spotify"]["can_remove_tracks"] is True
    assert rows["spotify"]["saved_albums"] == {"read": True, "write": True}
    assert rows["spotify"]["followed_artists"] == {
        "read": True,
        "write": True,
        "semantics": "follow",
    }
    assert rows["tidal"]["can_source"] is True
    assert rows["tidal"]["can_target"] is True
    assert rows["tidal"]["has_isrc"] is True
    assert rows["tidal"]["saved_albums"] == {"read": True, "write": True}
    assert rows["tidal"]["followed_artists"]["semantics"] == "favorite"
    assert rows["tidal"]["can_unfollow_playlist"] is False
    assert rows["tidal"]["can_delete_playlist"] is True
    assert rows["tidal"]["can_remove_tracks"] is False
    assert rows["applemusic"]["can_source"] is True
    assert rows["applemusic"]["can_target"] is True
    assert rows["applemusic"]["can_unfollow_playlist"] is False
    assert rows["applemusic"]["can_delete_playlist"] is False
    assert rows["applemusic"]["can_remove_tracks"] is False
    assert rows["applemusic"]["auth_kind"] == "developer_user_token"
    assert rows["local_file"] == {
        "name": "local_file",
        "display_name": "Local playlist file",
        "auth_kind": "upload",
        "official": True,
        "stability": "stable",
        "has_isrc": True,
        "can_source": True,
        "can_target": False,
        "can_mirror": False,
        "mirror_unavailable_reason": (
            "Local files are available for one-time playlist migrations."
        ),
        "can_unfollow_playlist": False,
        "can_delete_playlist": False,
        "can_remove_tracks": False,
        "max_remove_batch": 0,
        "saved_albums": {"read": False, "write": False},
        "followed_artists": {"read": False, "write": False, "semantics": None},
        "warning": None,
    }
    assert rows["spotify"]["can_mirror"] is True
    assert rows["spotify"]["mirror_unavailable_reason"] is None
    assert rows["tidal"]["can_mirror"] is False
    assert rows["tidal"]["mirror_unavailable_reason"]
    assert rows["applemusic"]["saved_albums"] == {"read": False, "write": False}
