from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import (
    AccessDenied,
    AuthExpired,
    NotFound,
    ProviderAdapter,
    ProviderError,
    PublicPlaylistReader,
    PublicPlaylistRef,
)
from app.core.models import Playlist, PlaylistRef
from app.core.registry import get
from app.db import models as orm
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential
from app.imports.http import SafeHttpFetcher
from app.imports.models import ImportIssue, ResolvedPlaylistUrl
from app.imports.parser import ImportLimitExceeded, TextImportLimits, parse_track_text
from app.imports.urls import resolve_playlist_url
from app.settings import Settings, get_settings


class ImportContentError(ValueError):
    pass


class SourceConnectionRequired(ProviderError):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


class ImportService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter_getter: Callable[[str], ProviderAdapter] = get,
        fetcher_factory: Callable[[set[str]], SafeHttpFetcher] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._adapter_getter = adapter_getter
        self._fetcher_factory = fetcher_factory or self._safe_fetcher

    async def preview_text(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        text: str,
        name: str | None,
    ) -> orm.ImportedPlaylist:
        parsed = parse_track_text(
            text,
            name=name,
            limits=TextImportLimits(
                max_bytes=self._settings.import_max_text_bytes,
                max_items=self._settings.import_max_items,
                max_line_chars=self._settings.import_max_line_chars,
                max_field_chars=self._settings.import_max_field_chars,
            ),
        )
        if not parsed.playlist.tracks:
            raise ImportContentError("pasted text did not contain any valid tracks")
        return await self._persist(
            session,
            user_id=user_id,
            source_provider="text",
            source_label="Pasted text",
            source_locator=f"text:{parsed.fingerprint}",
            source_fingerprint=parsed.fingerprint,
            playlist=parsed.playlist,
            issues=parsed.issues,
        )

    async def preview_url(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        url: str,
        source_account_id: str | None,
    ) -> orm.ImportedPlaylist:
        resolved = resolve_playlist_url(
            url,
            open_playlist_hosts=self._settings.open_playlist_import_hosts,
            max_length=self._settings.import_max_url_chars,
        )
        if resolved.provider == "openplaylist":
            playlist = await self._read_open_playlist(resolved)
        else:
            playlist = await self._read_provider_playlist(
                session,
                user_id=user_id,
                resolved=resolved,
                source_account_id=source_account_id,
            )
        playlist = self._normalize_playlist(playlist, resolved)
        issues = self._unsupported_issues(playlist)
        fingerprint = hashlib.sha256(
            f"{resolved.provider}\0{resolved.canonical_url}".encode()
        ).hexdigest()
        return await self._persist(
            session,
            user_id=user_id,
            source_provider=resolved.provider,
            source_label=resolved.source_label,
            source_locator=resolved.canonical_url,
            source_fingerprint=fingerprint,
            playlist=playlist,
            issues=issues,
        )

    async def _read_provider_playlist(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        resolved: ResolvedPlaylistUrl,
        source_account_id: str | None,
    ) -> Playlist:
        adapter = self._adapter_getter(resolved.provider)
        if isinstance(adapter, PublicPlaylistReader):
            try:
                return await adapter.read_public_playlist(
                    PublicPlaylistRef(
                        id=resolved.resource_id,
                        canonical_url=resolved.canonical_url,
                        metadata=resolved.metadata,
                        max_items=self._settings.import_max_items,
                    )
                )
            except (AccessDenied, AuthExpired, NotFound) as exc:
                if resolved.provider == "ytmusic" and not source_account_id:
                    raise SourceConnectionRequired(
                        resolved.provider,
                        "This YouTube Music playlist is private or unavailable. "
                        "Connect YouTube Music and retry.",
                    ) from exc
                if resolved.provider != "ytmusic":
                    raise
        if not source_account_id:
            raise SourceConnectionRequired(
                resolved.provider,
                f"Connect {adapter.info.display_name} to read this playlist URL.",
            )
        try:
            credential, _ = await load_fresh_credential(
                session,
                account_id=source_account_id,
                adapter=adapter,
                provider=resolved.provider,
                user_id=user_id,
            )
        except (AccountNotFound, CredentialNotFound) as exc:
            raise SourceConnectionRequired(
                resolved.provider,
                f"Connect {adapter.info.display_name} to read this playlist URL.",
            ) from exc
        return await adapter.read_playlist(
            credential,
            PlaylistRef(id=resolved.resource_id, name=resolved.resource_id),
        )

    async def _read_open_playlist(self, resolved: ResolvedPlaylistUrl) -> Playlist:
        hosts = self._settings.open_playlist_import_hosts
        response = await self._fetcher_factory(hosts).fetch(resolved.metadata["fetch_url"])
        if response.status_code == 404:
            raise NotFound(resolved.canonical_url)
        if response.status_code in {401, 403}:
            raise AccessDenied("Open Playlist Engine share is not public")
        if response.status_code != 200:
            raise ProviderError(
                f"Open Playlist Engine returned HTTP {response.status_code}"
            )
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            raise ImportContentError("Open Playlist Engine response was not JSON")
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportContentError(
                "Open Playlist Engine response contained invalid JSON"
            ) from exc
        if isinstance(payload, dict) and isinstance(payload.get("playlist"), dict):
            payload = payload["playlist"]
        if not isinstance(payload, dict):
            raise ImportContentError("Open Playlist Engine response did not contain a playlist")
        raw_tracks = payload.get("tracks")
        if not isinstance(raw_tracks, list):
            raise ImportContentError("Open Playlist Engine playlist did not contain tracks")
        if len(raw_tracks) > self._settings.import_max_items:
            raise ImportLimitExceeded(
                f"playlist exceeds the {self._settings.import_max_items} items input limit"
            )
        normalized = dict(payload)
        normalized["tracks"] = [_normalize_open_track(item) for item in raw_tracks]
        try:
            return Playlist.model_validate(normalized)
        except ValueError as exc:
            raise ImportContentError(f"Open Playlist Engine playlist is invalid: {exc}") from exc

    def _normalize_playlist(
        self,
        playlist: Playlist,
        resolved: ResolvedPlaylistUrl,
    ) -> Playlist:
        if len(playlist.tracks) > self._settings.import_max_items:
            raise ImportLimitExceeded(
                f"playlist exceeds the {self._settings.import_max_items} items input limit"
            )
        playlist_id = resolved.resource_id
        if resolved.provider == "openplaylist":
            playlist_id = (
                "openplaylist:"
                + hashlib.sha256(resolved.canonical_url.encode()).hexdigest()[:32]
            )
        tracks = [
            track.model_copy(
                update={
                    "position": track.position if track.position is not None else position,
                    "source_item_id": track.source_item_id
                    or track.id
                    or f"{resolved.provider}:{resolved.resource_id}:{position}",
                }
            )
            for position, track in enumerate(playlist.tracks)
        ]
        return playlist.model_copy(update={"id": playlist_id, "tracks": tracks})

    def _unsupported_issues(self, playlist: Playlist) -> list[ImportIssue]:
        return [
            ImportIssue(
                code="unsupported_item",
                message=(
                    f"{track.title}: "
                    f"{track.unsupported_reason or 'unsupported playlist item'}"
                ),
                severity="warning",
            )
            for track in playlist.tracks
            if not track.is_migratable
        ]

    async def _persist(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        source_provider: str,
        source_label: str,
        source_locator: str,
        source_fingerprint: str,
        playlist: Playlist,
        issues: list[ImportIssue],
    ) -> orm.ImportedPlaylist:
        row = orm.ImportedPlaylist(
            user_id=user_id,
            source_provider=source_provider,
            source_label=source_label,
            source_locator=source_locator,
            source_fingerprint=source_fingerprint,
            playlist_id=playlist.id or source_fingerprint,
            playlist=playlist.model_dump(mode="json"),
            issues=[issue.model_dump(mode="json") for issue in issues],
        )
        session.add(row)
        await session.flush()
        return row

    def _safe_fetcher(self, hosts: set[str]) -> SafeHttpFetcher:
        return SafeHttpFetcher(
            allowed_hosts=hosts,
            max_redirects=self._settings.import_max_redirects,
            max_response_bytes=self._settings.import_max_response_bytes,
            timeout_s=self._settings.import_http_timeout_s,
        )


def _normalize_open_track(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    track = dict(value)
    if "duration_s" not in track and "duration" in track:
        track["duration_s"] = track.pop("duration")
    return track
