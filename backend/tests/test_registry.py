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
    assert rows["applemusic"]["can_source"] is True
    assert rows["applemusic"]["can_target"] is True
    assert rows["applemusic"]["auth_kind"] == "developer_user_token"
    assert rows["applemusic"]["saved_albums"] == {"read": False, "write": False}
