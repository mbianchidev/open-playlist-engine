from __future__ import annotations


def test_generator_config_is_disabled_without_admin_endpoint(client) -> None:
    response = client.get("/api/generator/config")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "backend": "openai_compatible",
        "model": "",
        "message": (
            "Playlist generation is disabled. Configure OPE_GENERATOR_OPENAI_BASE_URL "
            "and OPE_GENERATOR_MODEL."
        ),
        "limits": {
            "max_prompt_chars": 2000,
            "max_output_chars": 32000,
            "max_tracks": 25,
        },
    }


def test_generator_routes_are_published_in_openapi(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/generator/config" in paths
    assert "/api/generator/drafts" in paths
    assert "/api/generator/drafts/{draft_id}/confirm" in paths
    assert "/api/generator/preferences" in paths
