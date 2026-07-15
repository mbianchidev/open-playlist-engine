"""Application settings.

A single ``DEPLOYMENT_MODE`` flag drives the self-host vs hosted differences
(see docs/DESIGN.md). Defaults target self-hosted single-user; the hosted path
tightens auth, encryption and the shared match graph.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit

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
    public_base_url: str = ""

    # Secrets. In hosted mode this should come from a KMS-backed KeyProvider.
    secret_key: str = "dev-only-change-me"
    owner_access_token: str = ""
    owner_session_ttl_s: int = 43_200

    # Public playlist sharing. Disabled until public_base_url and strong secrets are set.
    share_recipient_session_ttl_s: int = 86_400
    share_recipient_credential_retention_s: int = 86_400
    share_max_tracks: int = 1_000
    share_max_snapshot_bytes: int = 2_000_000
    share_max_download_bytes: int = 4_000_000
    share_max_expiry_days: int = 365
    share_rate_limit_capacity: int = 60
    share_rate_limit_refill_per_s: float = 1.0
    share_import_max_concurrent_jobs: int = 3
    share_import_daily_track_limit: int = 1_000
    share_artwork_hosts: str = (
        "i.scdn.co,mosaic.scdn.co,i.ytimg.com,lh3.googleusercontent.com,"
        "resources.tidal.com,mzstatic.com"
    )

    # Provider write paths
    ytmusic_enabled: bool = True
    youtube_official_enabled: bool = False
    ytmusic_client_id: str = ""
    ytmusic_client_secret: str = ""
    ytmusic_device_code_url: str = "https://www.youtube.com/o/oauth2/device/code"
    ytmusic_token_url: str = "https://oauth2.googleapis.com/token"
    ytmusic_userinfo_url: str = "https://www.googleapis.com/oauth2/v2/userinfo"

    # Match graph: per-user/local by default. Sharing globally needs legal review.
    enable_shared_graph: bool = False
    review_confidence_threshold: float = 0.8

    # Conservative migration guardrails. Users can override after acknowledging warnings.
    migration_safe_max_playlists_per_job: int = 1
    migration_safe_max_tracks_per_job: int = 50
    migration_safe_daily_tracks: int = 250
    migration_safe_min_job_gap_s: int = 120
    migration_worker_job_timeout_s: int = 3600

    # Spotify OAuth (set in .env)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8000/api/auth/spotify/callback"

    # Tidal OAuth (set in .env)
    tidal_client_id: str = ""
    tidal_client_secret: str = ""
    tidal_redirect_uri: str = "http://127.0.0.1:8000/api/auth/tidal/callback"

    # Apple MusicKit developer token (set in .env)
    apple_music_team_id: str = ""
    apple_music_key_id: str = ""
    apple_music_private_key: str = ""
    apple_music_private_key_path: str = ""
    apple_music_token_ttl_s: int = 86_400

    @property
    def is_hosted(self) -> bool:
        return self.deployment_mode is DeploymentMode.HOSTED

    @property
    def allow_header_paste(self) -> bool:
        """Pasting provider session headers/cookies is only safe when self-hosted."""
        return not self.is_hosted

    @property
    def public_base_url_normalized(self) -> str:
        return self.public_base_url.strip().rstrip("/")

    @property
    def owner_auth_required(self) -> bool:
        return bool(self.public_base_url_normalized)

    @property
    def sharing_enabled(self) -> bool:
        return not self.sharing_disabled_reason

    @property
    def sharing_disabled_reason(self) -> str:
        public_url = self.public_base_url_normalized
        if not public_url:
            return "Set OPE_PUBLIC_BASE_URL to enable public playlist sharing."
        parsed = urlsplit(public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "OPE_PUBLIC_BASE_URL must be an absolute HTTP or HTTPS URL."
        if len(self.owner_access_token) < 32:
            return "Set OPE_OWNER_ACCESS_TOKEN to at least 32 characters."
        if len(self.secret_key) < 32 or self.secret_key in {
            "dev-only-change-me",
            "change-me-please",
        }:
            return "Set OPE_SECRET_KEY to a strong value of at least 32 characters."
        return ""

    @property
    def approved_share_artwork_hosts(self) -> set[str]:
        return {
            host.strip().lower().strip(".")
            for host in self.share_artwork_hosts.split(",")
            if host.strip()
        }

    @property
    def secure_public_cookies(self) -> bool:
        return urlsplit(self.public_base_url_normalized).scheme == "https"

    def recipient_redirect_ready(self, provider: str) -> bool:
        redirect_uri = {
            "spotify": self.spotify_redirect_uri,
            "tidal": self.tidal_redirect_uri,
        }.get(provider)
        if redirect_uri is None:
            return True
        public = urlsplit(self.public_base_url_normalized)
        redirect = urlsplit(redirect_uri)
        return (
            public.scheme == redirect.scheme
            and public.hostname == redirect.hostname
            and _effective_port(public.scheme, public.port)
            == _effective_port(redirect.scheme, redirect.port)
        )


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(scheme)


@lru_cache
def get_settings() -> Settings:
    return Settings()
