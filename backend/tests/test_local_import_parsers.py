from __future__ import annotations

import builtins
from io import BytesIO
from pathlib import Path

import pytest

from app.imports.models import ImportFormat, ImportLimits
from app.imports.registry import ImportLimitExceeded, PlaylistImportError, parse_playlist_file

FIXTURES = Path(__file__).parent / "fixtures" / "local_imports"
VALID_FORMATS = [
    (ImportFormat.TXT, "txt"),
    (ImportFormat.CSV, "csv"),
    (ImportFormat.M3U, "m3u"),
    (ImportFormat.M3U8, "m3u8"),
    (ImportFormat.PLS, "pls"),
    (ImportFormat.WPL, "wpl"),
    (ImportFormat.XSPF, "xspf"),
    (ImportFormat.XML, "xml"),
    (ImportFormat.JSON, "json"),
]


def _parse_fixture(name: str, *, max_tracks: int = 100):
    with (FIXTURES / name).open("rb") as source:
        return parse_playlist_file(
            source,
            filename=name,
            limits=ImportLimits(
                max_upload_bytes=1_000_000,
                max_playlists=10,
                max_tracks=max_tracks,
                max_issues=100,
            ),
        )


@pytest.mark.parametrize(("expected_format", "extension"), VALID_FORMATS)
def test_supported_format_fixture_preserves_unicode_and_reports_loss(
    expected_format: ImportFormat,
    extension: str,
) -> None:
    result = _parse_fixture(f"valid.{extension}")

    assert result.detected_format is expected_format
    assert len(result.playlists) == 1
    assert result.playlists[0].name == "Road Trip"
    assert [track.position for track in result.playlists[0].tracks] == list(
        range(len(result.playlists[0].tracks))
    )
    assert any(
        track.title == "Déjà Vu" and track.artist == "Beyoncé"
        for track in result.playlists[0].tracks
    )
    assert all(track.source_item_id for track in result.playlists[0].tracks)
    assert result.duplicate_count == 1
    assert result.malformed_count >= 1
    assert result.unsupported_count >= 1
    assert any(issue.line_or_item is not None for issue in result.issues)


@pytest.mark.parametrize(("expected_format", "extension"), VALID_FORMATS)
def test_supported_format_fixture_rejects_malformed_document(
    expected_format: ImportFormat,
    extension: str,
) -> None:
    with pytest.raises(PlaylistImportError) as exc_info:
        _parse_fixture(f"malformed.{extension}")

    assert exc_info.value.format in {expected_format, None}
    assert exc_info.value.code in {
        "empty_import",
        "invalid_document",
        "invalid_encoding",
        "missing_columns",
    }


@pytest.mark.parametrize(("expected_format", "extension"), VALID_FORMATS)
def test_supported_format_fixture_enforces_track_limit(
    expected_format: ImportFormat,
    extension: str,
) -> None:
    with pytest.raises(ImportLimitExceeded) as exc_info:
        _parse_fixture(f"valid.{extension}", max_tracks=2)

    assert exc_info.value.format is expected_format
    assert exc_info.value.code == "track_limit"


def test_content_detection_overrides_mismatched_extension() -> None:
    payload = (FIXTURES / "valid.json").read_bytes()

    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlist.txt",
        limits=ImportLimits(),
    )

    assert result.detected_format is ImportFormat.JSON
    assert any(issue.code == "extension_mismatch" for issue in result.issues)


@pytest.mark.parametrize(
    ("payload", "filename", "expected_encoding"),
    [
        (
            "\ufeff#PLAYLIST:Road Trip\r\nBeyoncé - Déjà Vu\r\n".encode(),
            "playlist.txt",
            "utf-8-sig",
        ),
        (
            "Playlist,Title,Artist\r\nRoad Trip,Déjà Vu,Beyoncé\r\n".encode("utf-16"),
            "playlist.csv",
            "utf-16",
        ),
        (
            (
                "#EXTM3U\n#PLAYLIST:Road Trip\n"
                "#EXTINF:239,Beyoncé - Déjà Vu\nspotify:track:abc\n"
            ).encode("cp1252"),
            "playlist.m3u",
            "cp1252",
        ),
    ],
)
def test_text_formats_handle_bom_and_fallback_encodings(
    payload: bytes,
    filename: str,
    expected_encoding: str,
) -> None:
    result = parse_playlist_file(BytesIO(payload), filename=filename, limits=ImportLimits())

    assert result.encoding == expected_encoding
    assert result.playlists[0].tracks[0].artist == "Beyoncé"


def test_m3u8_requires_utf8() -> None:
    payload = "#EXTM3U\n#EXTINF:239,Beyoncé - Déjà Vu\nspotify:track:abc\n".encode("cp1252")

    with pytest.raises(PlaylistImportError) as exc_info:
        parse_playlist_file(BytesIO(payload), filename="playlist.m3u8", limits=ImportLimits())

    assert exc_info.value.code == "invalid_encoding"
    assert exc_info.value.format is ImportFormat.M3U8


def test_xml_rejects_doctype_and_external_entities() -> None:
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE playlist [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<playlist><title>&xxe;</title></playlist>
"""

    with pytest.raises(PlaylistImportError) as exc_info:
        parse_playlist_file(BytesIO(payload), filename="playlist.xml", limits=ImportLimits())

    assert exc_info.value.code == "unsafe_xml"


def test_local_paths_are_annotated_without_being_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    source = (FIXTURES / "valid.m3u").open("rb")
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        value = str(file)
        if value.startswith(("file://", "/Users/", "..", "\\\\")):
            raise AssertionError(f"parser attempted to open playlist entry: {value}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        result = parse_playlist_file(source, filename="valid.m3u", limits=ImportLimits())
    finally:
        source.close()

    local_tracks = [track for track in result.playlists[0].tracks if track.is_local]
    assert len(local_tracks) == 1
    assert local_tracks[0].unsupported_reason


def test_issue_limit_is_bounded_and_reported() -> None:
    rows = ["#PLAYLIST:Broken"] + [f"Title only {index}" for index in range(20)]
    result = parse_playlist_file(
        BytesIO("\n".join(rows).encode()),
        filename="broken.txt",
        limits=ImportLimits(max_issues=3),
    )

    assert len(result.issues) == 4
    assert result.issues[-1].code == "issue_limit"
    assert result.malformed_count == 20


def test_empty_file_has_actionable_error() -> None:
    with pytest.raises(PlaylistImportError) as exc_info:
        parse_playlist_file(BytesIO(b""), filename="empty.csv", limits=ImportLimits())

    assert exc_info.value.code == "empty_import"
    assert "no playlist tracks" in str(exc_info.value).lower()


def test_json_keeps_valid_playlist_when_sibling_is_malformed() -> None:
    payload = b"""[
      {"name": "Good", "tracks": [{"title": "Song", "artist": "Artist"}]},
      {"name": "Broken"}
    ]"""

    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlists.json",
        limits=ImportLimits(),
    )

    assert [playlist.name for playlist in result.playlists] == ["Good"]
    assert result.playlists[0].tracks[0].title == "Song"
    assert result.malformed_count == 1
    assert any(issue.code == "empty_playlist" for issue in result.issues)


def test_empty_playlists_count_toward_playlist_limit() -> None:
    payload = b"""{
      "playlists": [
        {"name": "Empty", "tracks": []},
        {"name": "Good", "tracks": [{"title": "Song", "artist": "Artist"}]}
      ]
    }"""

    with pytest.raises(ImportLimitExceeded) as exc_info:
        parse_playlist_file(
            BytesIO(payload),
            filename="playlists.json",
            limits=ImportLimits(max_playlists=1),
        )

    assert exc_info.value.code == "playlist_limit"


def test_invalid_duration_is_reported_without_dropping_track() -> None:
    payload = b"title,artist,duration\nSong,Artist,not-a-duration\n"

    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlist.csv",
        limits=ImportLimits(),
    )

    assert result.playlists[0].tracks[0].duration_s is None
    assert result.playlists[0].tracks[0].is_migratable is True
    assert result.malformed_count == 1
    assert any(issue.code == "invalid_duration" for issue in result.issues)


def test_relative_audio_path_is_unsupported_local_entry() -> None:
    payload = (
        b"#EXTM3U\n#EXTINF:180,Artist - Song\nMusic/local.mp3\n"
    )

    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlist.m3u",
        limits=ImportLimits(),
    )

    track = result.playlists[0].tracks[0]
    assert track.is_local is True
    assert track.is_migratable is False
    assert any(issue.code == "unsupported_local_file" for issue in result.issues)


@pytest.mark.parametrize(
    ("payload", "filename", "expected_code"),
    [
        (b"\xff\xfeA", "playlist.txt", "invalid_encoding"),
        (
            b"title,artist\n" + b"x" * 200_000 + b",Artist\n",
            "playlist.csv",
            "invalid_document",
        ),
    ],
)
def test_decoder_and_csv_failures_are_actionable_import_errors(
    payload: bytes,
    filename: str,
    expected_code: str,
) -> None:
    with pytest.raises(PlaylistImportError) as exc_info:
        parse_playlist_file(
            BytesIO(payload),
            filename=filename,
            limits=ImportLimits(max_upload_bytes=300_000),
        )

    assert exc_info.value.code == expected_code


def test_xml_element_limit_is_checked_before_materializing_document() -> None:
    payload = ("<playlist>" + "<group>" * 1_001 + "</group>" * 1_001 + "</playlist>").encode()

    with pytest.raises(ImportLimitExceeded) as exc_info:
        parse_playlist_file(
            BytesIO(payload),
            filename="playlist.xml",
            limits=ImportLimits(max_tracks=1),
        )

    assert exc_info.value.code == "xml_element_limit"


def test_top_level_json_tracks_enforce_streaming_track_limit() -> None:
    payload = b'{"name":"Too many","tracks":[' + b",".join(
        b'{"title":"Song","artist":"Artist"}' for _ in range(3)
    ) + b"]}"

    with pytest.raises(ImportLimitExceeded) as exc_info:
        parse_playlist_file(
            BytesIO(payload),
            filename="playlist.json",
            limits=ImportLimits(max_tracks=2),
        )

    assert exc_info.value.code == "track_limit"


@pytest.mark.parametrize(
    ("payload", "expected_encoding"),
    [
        (
            ("\ufeff" + '{"tracks":[{"title":"Song","artist":"Artist"}]}').encode(),
            "utf-8-sig",
        ),
        (
            '{"tracks":[{"title":"Song","artist":"Artist"}]}'.encode("utf-16"),
            "utf-16",
        ),
        (
            '{"tracks":[{"title":"Song","artist":"Artist"}]}'.encode("utf-32"),
            "utf-32",
        ),
    ],
)
def test_json_accepts_bom_encoded_documents(
    payload: bytes,
    expected_encoding: str,
) -> None:
    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlist.json",
        limits=ImportLimits(),
    )

    assert result.playlists[0].tracks[0].title == "Song"
    assert result.encoding == expected_encoding


@pytest.mark.parametrize("duration", ["nan", "inf", "1e9999"])
def test_non_finite_duration_is_reported(duration: str) -> None:
    payload = f"title,artist,duration\nSong,Artist,{duration}\n".encode()

    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlist.csv",
        limits=ImportLimits(),
    )

    assert result.playlists[0].tracks[0].duration_s is None
    assert any(issue.code == "invalid_duration" for issue in result.issues)


def test_non_finite_json_duration_is_reported() -> None:
    payload = b'{"tracks":[{"title":"Song","artist":"Artist","duration":1e9999}]}'

    result = parse_playlist_file(
        BytesIO(payload),
        filename="playlist.json",
        limits=ImportLimits(),
    )

    assert result.playlists[0].tracks[0].duration_s is None
    assert any(issue.code == "invalid_duration" for issue in result.issues)
