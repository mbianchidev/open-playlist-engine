"""Application settings.

A single ``DEPLOYMENT_MODE`` flag drives the self-host vs hosted differences
(see docs/DESIGN.md). Defaults target self-hosted single-user; the hosted path
tightens auth, encryption and the shared match graph.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(StrEnum):
    SELF_HOST = "self_host"
    HOSTED = "hosted"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OPE_", extra="ignore")

    deployment_mode: DeploymentMode = DeploymentMode.SELF_HOST

    # Infra
    database_url: str = "postgresql+psycopg://ope:ope@localhost:5432/ope"
    valkey_url: str = "redis://localhost:6379/0"
    frontend_url: str = "http://localhost:8080"

    # Secrets. In hosted mode this should come from a KMS-backed KeyProvider.
    secret_key: str = "dev-only-change-me"

    # Provider write paths
    ytmusic_enabled: bool = True
    youtube_official_enabled: bool = False

    # Match graph: per-user/local by default. Sharing globally needs legal review.
    enable_shared_graph: bool = False
    review_confidence_threshold: float = 0.8

    # Spotify OAuth (set in .env)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8000/api/auth/spotify/callback"

    @property
    def is_hosted(self) -> bool:
        return self.deployment_mode is DeploymentMode.HOSTED

    @property
    def allow_header_paste(self) -> bool:
        """Pasting provider session headers/cookies is only safe when self-hosted."""
        return not self.is_hosted


@lru_cache
def get_settings() -> Settings:
    return Settings()
