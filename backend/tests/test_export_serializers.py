from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.core.models import MediaType, Playlist, PlaylistKind, Track
from app.exports.models import ExportFormat, ExportWarning
from app.exports.serializers import (
    OPE_XSPF_NAMESPACE,
    XSPF_NAMESPACE,
    FilenameAllocator,
    JsonBundleWriter,
    parse_open_playlist_bundle,
    safe_filename,
    write_playlist,
)


def _rich_playlist(*, playlist_id: str = "playlist-1", name: str = "Road Trip") -> Playlist:
    return Playlist(
        id=playlist_id,
        name=name,
        description="Ordered source playlist",
        photo="https://images.example/playlist.jpg",
        owner_id="owner-1",
        created_at=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
        updated_at=datetime(2024, 2, 3, 4, 5, tzinfo=UTC),
        kind=PlaylistKind.STANDARD,
        tracks=[
            Track(
                id="track-1",
                title='=HYPERLINK("https://evil.example","click")',
                artist="Artist & Co",
                album="Album <One>",
                duration_s=201,
                release_date=date(2020, 3, 4),
                isrc="USAAA2000001",
                artwork_uri="https://images.example/track.jpg",
                provider_uris={"spotify": "spotify:track:abc123"},
                metadata={"provider_rank": 7},
                position=0,
                source_item_id="source-1",
                added_at=datetime(2024, 3, 4, 5, 6, tzinfo=UTC),
            ),
            Track(
                id="track-2",
                title="Bell\x07 & < > \" '",
                artist="Podcast Host",
                album=None,
                duration_s=None,
                provider_uris={},
                position=1,
                media_type=MediaType.EPISODE,
                source_item_id="source-2",
                unsupported_reason="Episodes are not migratable",
            ),
        ],
    )


def test_json_bundle_round_trip_preserves_multiple_playlists_and_empty_playlist() -> None:
    playlists = [_rich_playlist(), Playlist(id="empty", name="Empty", tracks=[])]
    stream = io.StringIO()
    writer = JsonBundleWriter(stream, source_provider="spotify")
    for playlist in playlists:
        writer.write_playlist(playlist)
    writer.finish(
        [
            ExportWarning(
                code="unsupported_items",
                message="One item is unsupported.",
                playlist_id="playlist-1",
            )
        ]
    )

    bundle = parse_open_playlist_bundle(stream.getvalue())

    assert bundle.source_provider == "spotify"
    assert bundle.playlists == playlists
    assert bundle.warnings[0].code == "unsupported_items"
    assert bundle.model_dump(mode="json", by_alias=True)["$schema"].endswith(
        "open-playlist-bundle-v1.json"
    )


def test_json_bundle_rejects_unknown_schema_version() -> None:
    stream = io.StringIO()
    writer = JsonBundleWriter(stream, source_provider="spotify")
    writer.write_playlist(_rich_playlist())
    writer.finish([])
    payload = json.loads(stream.getvalue())
    payload["schema_version"] = 2

    with pytest.raises(ValidationError):
        parse_open_playlist_bundle(json.dumps(payload))


def test_csv_is_utf8_bom_ordered_and_neutralizes_spreadsheet_formulas() -> None:
    stream = io.StringIO()

    write_playlist(
        stream,
        ExportFormat.CSV,
        _rich_playlist(),
        source_provider="spotify",
    )

    payload = stream.getvalue()
    assert payload.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(payload.removeprefix("\ufeff"))))
    assert [row["order"] for row in rows] == ["1", "2"]
    assert rows[0]["title"].startswith("'=HYPERLINK")
    assert rows[0]["source_uri"] == "spotify:track:abc123"
    assert rows[0]["added_at"] == "2024-03-04T05:06:00Z"
    assert rows[1]["media_type"] == "episode"
    assert rows[1]["unsupported_reason"] == "Episodes are not migratable"


def test_csv_empty_playlist_keeps_playlist_metadata_in_a_valid_row() -> None:
    stream = io.StringIO()

    write_playlist(
        stream,
        ExportFormat.CSV,
        Playlist(id="empty", name="Empty", description="No tracks"),
        source_provider="spotify",
    )

    rows = list(csv.DictReader(io.StringIO(stream.getvalue().removeprefix("\ufeff"))))
    assert len(rows) == 1
    assert rows[0]["playlist_id"] == "empty"
    assert rows[0]["playlist_description"] == "No tracks"
    assert rows[0]["order"] == ""
    assert rows[0]["title"] == ""


def test_txt_is_tab_delimited_and_ordered() -> None:
    stream = io.StringIO()

    write_playlist(
        stream,
        ExportFormat.TXT,
        _rich_playlist(),
        source_provider="spotify",
    )

    lines = stream.getvalue().splitlines()
    assert lines[:2] == ["# Open Playlist TXT v1", "# Encoding: UTF-8"]
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[2:])), delimiter="\t"))
    assert [row["order"] for row in rows] == ["1", "2"]
    assert rows[0]["title"].startswith("'=HYPERLINK")


def test_m3u8_preserves_order_and_normalizes_provider_urls() -> None:
    stream = io.StringIO()

    write_playlist(
        stream,
        ExportFormat.M3U8,
        _rich_playlist(),
        source_provider="spotify",
    )

    lines = stream.getvalue().splitlines()
    assert lines[0] == "#EXTM3U"
    assert "#OPE-SCHEMA:open-playlist-m3u-v1" in lines
    assert "https://open.spotify.com/track/abc123" in lines
    assert "#OPE-ORDER:1" in lines
    assert "#OPE-ORDER:2" in lines
    assert "#OPE-MISSING-URI" in lines
    assert lines.index("#OPE-ORDER:1") < lines.index("#OPE-ORDER:2")


def test_xspf_is_valid_xml_with_extensions_and_illegal_controls_removed() -> None:
    stream = io.StringIO()

    write_playlist(
        stream,
        ExportFormat.XSPF,
        _rich_playlist(),
        source_provider="spotify",
    )

    root = ET.fromstring(stream.getvalue())
    tracks = root.findall(f".//{{{XSPF_NAMESPACE}}}track")
    assert len(tracks) == 2
    assert tracks[0].findtext(f"{{{XSPF_NAMESPACE}}}location") == (
        "https://open.spotify.com/track/abc123"
    )
    assert tracks[1].findtext(f"{{{XSPF_NAMESPACE}}}title") == "Bell & < > \" '"
    assert tracks[0].findtext(f".//{{{OPE_XSPF_NAMESPACE}}}order") == "1"
    assert tracks[1].findtext(f".//{{{OPE_XSPF_NAMESPACE}}}unsupportedReason") == (
        "Episodes are not migratable"
    )


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        ("../../etc/passwd", "playlist-id", "etc-passwd"),
        ("..", "playlist-id", "playlist-id"),
        ("/", "playlist-id", "playlist-id"),
        ("\\", "playlist-id", "playlist-id"),
        ("", "playlist-id", "playlist-id"),
        ("🎵", "playlist-id", "playlist-id"),
        ("CON", "playlist-id", "playlist-CON"),
        ('bad\r\n"name"', "playlist-id", "bad-name"),
    ],
)
def test_safe_filename_is_cross_platform_and_non_empty(
    value: str,
    fallback: str,
    expected: str,
) -> None:
    assert safe_filename(value, fallback=fallback) == expected


def test_filename_allocator_handles_case_insensitive_collisions() -> None:
    allocator = FilenameAllocator()

    assert allocator.allocate("Mix", extension="csv", fallback="one") == "Mix.csv"
    assert allocator.allocate("mix", extension="csv", fallback="two") == "mix-2.csv"
