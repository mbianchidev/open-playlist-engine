from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET

import pytest

from app.core.models import MediaType, Playlist, Track
from app.core.sharing import (
    PortableFormat,
    ShareVisibility,
    SnapshotLimitError,
    build_shared_snapshot,
    generate_share_token,
    hash_share_token,
    render_share_html,
    serialize_snapshot,
    snapshot_to_playlist,
)


def _playlist() -> Playlist:
    return Playlist(
        id="private-provider-playlist-id",
        name='Road </title><script>alert("x")</script>',
        description="Owner description\r\nwith markup <b>and detail</b>",
        photo="https://i.scdn.co/image/approved-cover",
        owner_id="private-owner-account-id",
        snapshot_id="private-provider-snapshot",
        tracks=[
            Track(
                id="private-track-id",
                title="=HYPERLINK(\"https://evil.example\")\nInjected",
                artist="+Artist\rName",
                album="@Album",
                duration_s=187,
                isrc="USABC1234567",
                artwork_uri="http://127.0.0.1/private-art",
                provider_uris={"spotify": "spotify:track:abc123"},
                metadata={
                    "access_token": "must-not-leak",
                    "provider_account_id": "must-not-leak",
                },
                position=0,
            ),
            Track(
                title="Unsupported episode",
                artist="Publisher",
                media_type=MediaType.EPISODE,
                unsupported_reason="Podcast episodes cannot be migrated",
                position=1,
            ),
        ],
    )


def test_share_tokens_are_high_entropy_and_hash_lookup_does_not_store_plaintext() -> None:
    tokens = {generate_share_token() for _ in range(128)}

    assert len(tokens) == 128
    assert all(len(token) >= 43 for token in tokens)
    for token in tokens:
        digest = hash_share_token(token)
        assert len(digest) == 64
        assert token not in digest


def test_shared_snapshot_is_immutable_bounded_and_strips_private_provider_data() -> None:
    playlist = _playlist()
    snapshot = build_shared_snapshot(
        playlist,
        provider="spotify",
        playlist_id=playlist.id or "",
        attribution="Shared by Matteo",
        approved_artwork_hosts={"i.scdn.co"},
        max_tracks=10,
        max_bytes=100_000,
    )
    before = snapshot.model_dump(mode="json")

    playlist.name = "Changed later"
    playlist.tracks[0].title = "Changed later"
    playlist.tracks.append(Track(title="Added later", artist="Someone"))

    assert snapshot.model_dump(mode="json") == before
    assert snapshot.name.startswith("Road")
    assert snapshot.attribution == "Shared by Matteo"
    assert snapshot.source.provider == "spotify"
    assert snapshot.source.url == "https://open.spotify.com/playlist/private-provider-playlist-id"
    assert snapshot.cover_url == "https://i.scdn.co/image/approved-cover"
    assert snapshot.tracks[0].artwork_url is None
    assert snapshot.tracks[0].source_url == "https://open.spotify.com/track/abc123"

    raw = json.dumps(before)
    assert "private-owner-account-id" not in raw
    assert "private-provider-snapshot" not in raw
    assert "private-track-id" not in raw
    assert "access_token" not in raw
    assert "provider_account_id" not in raw

    imported = snapshot_to_playlist(snapshot)
    assert imported.name == snapshot.name
    assert imported.id is None
    assert imported.owner_id is None
    assert imported.snapshot_id is None
    assert imported.tracks[0].provider_uris == {}


def test_snapshot_limits_fail_closed() -> None:
    with pytest.raises(SnapshotLimitError, match="track limit"):
        build_shared_snapshot(
            _playlist(),
            provider="spotify",
            playlist_id="playlist",
            attribution=None,
            approved_artwork_hosts={"i.scdn.co"},
            max_tracks=1,
            max_bytes=100_000,
        )

    with pytest.raises(SnapshotLimitError, match="byte limit"):
        build_shared_snapshot(
            _playlist(),
            provider="spotify",
            playlist_id="playlist",
            attribution=None,
            approved_artwork_hosts={"i.scdn.co"},
            max_tracks=10,
            max_bytes=100,
        )


def test_open_graph_html_escapes_metadata_and_does_not_include_tracks() -> None:
    snapshot = build_shared_snapshot(
        _playlist(),
        provider="spotify",
        playlist_id="playlist",
        attribution='Shared by "owner" <admin@example.com>',
        approved_artwork_hosts={"i.scdn.co"},
        max_tracks=10,
        max_bytes=100_000,
    )

    page = render_share_html(
        snapshot,
        canonical_url="https://music.example/share/token",
        app_url="https://music.example/shared/token",
        visibility=ShareVisibility.UNLISTED,
    )

    assert '<script>alert("x")</script>' not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert 'content="noindex,nofollow"' in page
    assert "must-not-leak" not in page
    assert snapshot.tracks[0].title not in page


@pytest.mark.parametrize(
    ("format_", "suffix", "media_type"),
    [
        (PortableFormat.JSON, ".json", "application/json"),
        (PortableFormat.CSV, ".csv", "text/csv"),
        (PortableFormat.TXT, ".txt", "text/plain"),
        (PortableFormat.M3U8, ".m3u8", "audio/x-mpegurl"),
        (PortableFormat.XSPF, ".xspf", "application/xspf+xml"),
    ],
)
def test_portable_exports_are_bounded_and_well_typed(
    format_: PortableFormat, suffix: str, media_type: str
) -> None:
    snapshot = build_shared_snapshot(
        _playlist(),
        provider="spotify",
        playlist_id="playlist",
        attribution=None,
        approved_artwork_hosts={"i.scdn.co"},
        max_tracks=10,
        max_bytes=100_000,
    )

    exported = serialize_snapshot(snapshot, format_, max_bytes=100_000)

    assert exported.filename.endswith(suffix)
    assert exported.media_type == media_type
    assert 0 < len(exported.content) <= 100_000


def test_portable_exports_escape_structural_and_spreadsheet_injection() -> None:
    snapshot = build_shared_snapshot(
        _playlist(),
        provider="spotify",
        playlist_id="playlist",
        attribution=None,
        approved_artwork_hosts={"i.scdn.co"},
        max_tracks=10,
        max_bytes=100_000,
    )

    csv_export = serialize_snapshot(snapshot, PortableFormat.CSV, max_bytes=100_000)
    rows = list(csv.DictReader(io.StringIO(csv_export.content.decode("utf-8-sig"))))
    assert rows[0]["title"].startswith("'=")
    assert "\n" not in rows[0]["title"]
    assert rows[0]["artist"].startswith("'+")
    assert rows[0]["album"].startswith("'@")

    text_export = serialize_snapshot(snapshot, PortableFormat.TXT, max_bytes=100_000)
    text = text_export.content.decode()
    assert "Injected" in text
    assert "\r" not in text

    m3u_export = serialize_snapshot(snapshot, PortableFormat.M3U8, max_bytes=100_000)
    m3u = m3u_export.content.decode()
    assert m3u.startswith("#EXTM3U\n")
    assert "#EXTINF:187,+Artist Name - =HYPERLINK" in m3u

    xspf_export = serialize_snapshot(snapshot, PortableFormat.XSPF, max_bytes=100_000)
    ET.fromstring(xspf_export.content)


