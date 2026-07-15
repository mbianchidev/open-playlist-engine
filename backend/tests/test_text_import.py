from __future__ import annotations

import pytest

from app.imports.parser import ImportLimitExceeded, TextImportLimits, parse_track_text


def _limits(**overrides: int) -> TextImportLimits:
    values = {
        "max_bytes": 10_000,
        "max_items": 20,
        "max_line_chars": 200,
        "max_field_chars": 100,
    }
    values.update(overrides)
    return TextImportLimits(**values)


def test_text_parser_supports_common_forms_unicode_comments_and_duplicates() -> None:
    parsed = parse_track_text(
        "\n".join(
            [
                "# Road trip",
                "Björk - Jóga",
                "Massive Attack\tTeardrop\tMezzanine\tGBBKS9700076",
                "Windowlicker",
                "Björk - Jóga",
            ]
        ),
        name="Road trip",
        limits=_limits(),
    )

    assert parsed.playlist.name == "Road trip"
    assert [(track.artist, track.title) for track in parsed.playlist.tracks] == [
        ("Björk", "Jóga"),
        ("Massive Attack", "Teardrop"),
        ("", "Windowlicker"),
        ("Björk", "Jóga"),
    ]
    assert parsed.playlist.tracks[1].album == "Mezzanine"
    assert parsed.playlist.tracks[1].isrc == "GBBKS9700076"
    assert parsed.playlist.tracks[0].source_item_id == "text:2"
    assert parsed.playlist.tracks[3].position == 3
    assert [(issue.line, issue.code) for issue in parsed.issues] == [(4, "missing_artist")]


def test_text_parser_supports_headered_tabular_data_and_row_errors() -> None:
    parsed = parse_track_text(
        "\n".join(
            [
                "artist\ttitle\talbum\tisrc",
                "Portishead\tRoads\tDummy\tGBAQH9400008",
                "No title\t\tAlbum\t",
                "\tOnly title\tAlbum\t",
            ]
        ),
        name=None,
        limits=_limits(),
    )

    assert parsed.playlist.name == "Imported track list"
    assert [(track.artist, track.title) for track in parsed.playlist.tracks] == [
        ("Portishead", "Roads"),
        ("", "Only title"),
    ]
    assert [(issue.line, issue.code, issue.severity) for issue in parsed.issues] == [
        (3, "missing_title", "error"),
        (4, "missing_artist", "warning"),
    ]


def test_text_parser_reports_overlong_rows_without_losing_valid_rows() -> None:
    parsed = parse_track_text(
        f"{'x' * 21}\nArtist - Song",
        name="List",
        limits=_limits(max_line_chars=20),
    )

    assert [(track.artist, track.title) for track in parsed.playlist.tracks] == [
        ("Artist", "Song")
    ]
    assert [(issue.line, issue.code) for issue in parsed.issues] == [(1, "line_too_long")]


def test_text_parser_rejects_oversized_input() -> None:
    with pytest.raises(ImportLimitExceeded, match="bytes"):
        parse_track_text("Artist - Song", name=None, limits=_limits(max_bytes=4))


def test_text_parser_rejects_too_many_rows() -> None:
    with pytest.raises(ImportLimitExceeded, match="items"):
        parse_track_text(
            "A - One\nB - Two\nC - Three",
            name=None,
            limits=_limits(max_items=2),
        )

