"""Resolution logic for public playlist URL and pasted-text imports.

Kept separate from :mod:`app.imports.service` (which owns the provider
agnostic ``LocalPlaylistImport`` lease lifecycle shared with local-file
imports) to avoid circular imports and to keep the provider-network-facing
code -- URL parsing, the SSRF-safe fetcher, and provider adapter calls --
isolated from the lease bookkeeping.

Only two kinds of remote reads are ever performed:

* A same-adapter "public read" hook (:class:`app.core.adapter.PublicPlaylistReader`)
  for providers that expose an unauthenticated public playlist API
  (YouTube Music, Apple Music). If that is unavailable or the playlist turns
  out to be private, callers fall back to an owner-bound connected source
  account (Spotify, TIDAL, private YouTube Music playlists), surfaced as a
  structured :class:`SourceConnectionRequired` error so the frontend can
  prompt to connect.
* The current Open Playlist Engine share API, ``/api/public/shares/{token}``,
  whose JSON ``snapshot`` field is parsed strictly as a
  :class:`~app.core.sharing.SharedPlaylistSnapshot` and converted with
  :func:`~app.core.sharing.snapshot_to_playlist`. No HTML scraping is ever
  performed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
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
from app.core.sharing import SharedPlaylistSnapshot, snapshot_to_playlist
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential
from app.imports import PASTED_TEXT_PROVIDER
from app.imports.http import SafeHttpFetcher
from app.imports.models import ResolvedPlaylistUrl, SourceImportIssue
from app.imports.parser import ImportLimitExceeded, TextImportLimits, parse_track_text
from app.imports.urls import resolve_playlist_url
from app.settings import Settings

AdapterGetter = Callable[[str], ProviderAdapter]
FetcherFactory = Callable[[set[str], Settings], SafeHttpFetcher]


class ImportContentError(ValueError):
    pass


class SourceConnectionRequired(ProviderError):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


@dataclass(frozen=True)
class ExternalImportResult:
    """A normalized playlist resolved from a public URL or pasted text."""

    source_provider: str
    source_label: str
    source_locator: str
    source_fingerprint: str
    playlist: Playlist
    issues: list[SourceImportIssue] = field(default_factory=list)


async def resolve_text_import(
    text: str,
    *,
    name: str | None,
    settings: Settings,
) -> ExternalImportResult:
    parsed = parse_track_text(
        text,
        name=name,
        limits=TextImportLimits(
            max_bytes=settings.import_max_text_bytes,
            max_items=settings.import_max_items,
            max_line_chars=settings.import_max_line_chars,
            max_field_chars=settings.import_max_field_chars,
        ),
    )
    if not parsed.playlist.tracks:
        raise ImportContentError("pasted text did not contain any valid tracks")
    return ExternalImportResult(
        source_provider=PASTED_TEXT_PROVIDER,
        source_label="Pasted text",
        source_locator=f"text:{parsed.fingerprint}",
        source_fingerprint=parsed.fingerprint,
        playlist=parsed.playlist,
        issues=parsed.issues,
    )


async def resolve_url_import(
    session: AsyncSession,
    *,
    user_id: str,
    url: str,
    source_account_id: str | None,
    settings: Settings,
    adapter_getter: AdapterGetter = get,
    fetcher_factory: FetcherFactory | None = None,
) -> ExternalImportResult:
    resolved = resolve_playlist_url(
        url,
        open_playlist_hosts=settings.open_playlist_import_hosts,
        max_length=settings.import_max_url_chars,
    )
    if resolved.provider == "openplaylist":
        playlist = await _read_open_playlist(
            resolved,
            settings=settings,
            fetcher_factory=fetcher_factory or _safe_fetcher,
        )
    else:
        playlist = await _read_provider_playlist(
            session,
            user_id=user_id,
            resolved=resolved,
            source_account_id=source_account_id,
            settings=settings,
            adapter_getter=adapter_getter,
        )
    playlist = _normalize_playlist(playlist, resolved, settings=settings)
    issues = _unsupported_issues(playlist)
    fingerprint = hashlib.sha256(
        f"{resolved.provider}\0{resolved.canonical_url}".encode()
    ).hexdigest()
    return ExternalImportResult(
        source_provider=resolved.provider,
        source_label=resolved.source_label,
        source_locator=resolved.canonical_url,
        source_fingerprint=fingerprint,
        playlist=playlist,
        issues=issues,
    )


async def _read_provider_playlist(
    session: AsyncSession,
    *,
    user_id: str,
    resolved: ResolvedPlaylistUrl,
    source_account_id: str | None,
    settings: Settings,
    adapter_getter: AdapterGetter,
) -> Playlist:
    adapter = adapter_getter(resolved.provider)
    if isinstance(adapter, PublicPlaylistReader):
        try:
            return await adapter.read_public_playlist(
                PublicPlaylistRef(
                    id=resolved.resource_id,
                    canonical_url=resolved.canonical_url,
                    metadata=resolved.metadata,
                    max_items=settings.import_max_items,
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


async def _read_open_playlist(
    resolved: ResolvedPlaylistUrl,
    *,
    settings: Settings,
    fetcher_factory: FetcherFactory,
) -> Playlist:
    hosts = settings.open_playlist_import_hosts
    response = await fetcher_factory(hosts, settings).fetch(resolved.metadata["fetch_url"])
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
        payload: Any = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportContentError(
            "Open Playlist Engine response contained invalid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ImportContentError(
            "Open Playlist Engine response did not contain a playlist snapshot"
        )
    try:
        snapshot = SharedPlaylistSnapshot.model_validate(payload["snapshot"])
    except ValueError as exc:
        raise ImportContentError(
            f"Open Playlist Engine playlist snapshot is invalid: {exc}"
        ) from exc
    if len(snapshot.tracks) > settings.import_max_items:
        raise ImportLimitExceeded(
            f"playlist exceeds the {settings.import_max_items} items input limit"
        )
    return snapshot_to_playlist(snapshot)


def _normalize_playlist(
    playlist: Playlist,
    resolved: ResolvedPlaylistUrl,
    *,
    settings: Settings,
) -> Playlist:
    if len(playlist.tracks) > settings.import_max_items:
        raise ImportLimitExceeded(
            f"playlist exceeds the {settings.import_max_items} items input limit"
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


def _unsupported_issues(playlist: Playlist) -> list[SourceImportIssue]:
    return [
        SourceImportIssue(
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


def _safe_fetcher(hosts: set[str], settings: Settings) -> SafeHttpFetcher:
    return SafeHttpFetcher(
        allowed_hosts=hosts,
        max_redirects=settings.import_max_redirects,
        max_response_bytes=settings.import_max_response_bytes,
        timeout_s=settings.import_http_timeout_s,
    )
