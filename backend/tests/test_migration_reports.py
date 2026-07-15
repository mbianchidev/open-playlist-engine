from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from app.core.migration_reports import (
    REPORT_FIELDS,
    build_report_row,
    csv_header_chunk,
    csv_row_chunk,
    json_report_prefix,
    json_report_suffix,
)
from app.db import models as orm


def _job() -> orm.MigrationJob:
    return orm.MigrationJob(
        id="job-123",
        user_id="alice",
        source_provider="spotify",
        target_provider="ytmusic",
        source_account_id="spotify-account",
        target_account_id="ytmusic-account",
        selection={"playlist_ids": ["source-playlist"], "tracks": {}},
        status="done",
        error="One provider write failed",
        warnings=[{"code": "large_job", "message": "Large migration"}],
        created_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        started_at=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        completed_at=datetime(2026, 7, 14, 12, 3, tzinfo=UTC),
        details_expires_at=datetime(2026, 10, 12, 12, 3, tzinfo=UTC),
    )


def _item() -> orm.JobItem:
    return orm.JobItem(
        id="item-123",
        job_id="job-123",
        source_playlist_id="source-playlist",
        source_playlist_name="Road Trip",
        target_playlist_id="target-playlist",
        position=4,
        title="Song",
        artist="Artist",
        album="Album",
        duration_s=201,
        release_year=2024,
        explicit=True,
        isrc="US-ABC-24-12345",
        source_metadata={
            "id": "source-track",
            "source_item_id": "source-entry",
            "provider_uris": {"spotify": "spotify:track:source-track"},
        },
        target_uri="ytmusic:video:target-video",
        confidence=0.42,
        status="written",
        reason=None,
        review_action="approve",
        review_original_status="needs_review",
        review_original_reason="Low confidence",
        reviewed_at=datetime(2026, 7, 14, 12, 2, tzinfo=UTC),
        created_at=datetime(2026, 7, 14, 12, 1, 10, tzinfo=UTC),
        updated_at=datetime(2026, 7, 14, 12, 2, tzinfo=UTC),
    )


def test_report_row_has_stable_complete_fields() -> None:
    row = build_report_row(_job(), _item(), outcome="partial")

    assert tuple(row) == REPORT_FIELDS
    assert row["report_version"] == "1"
    assert row["job_id"] == "job-123"
    assert row["job_status"] == "done"
    assert row["job_outcome"] == "partial"
    assert row["job_error"] == "One provider write failed"
    assert row["job_warnings"] == [{"code": "large_job", "message": "Large migration"}]
    assert row["source_playlist_name"] == "Road Trip"
    assert row["source_track_id"] == "source-track"
    assert row["source_item_id"] == "source-entry"
    assert row["source_uri"] == "spotify:track:source-track"
    assert row["target_id"] == "target-video"
    assert row["review_action"] == "approve"
    assert row["review_original_reason"] == "Low confidence"
    assert row["item_updated_at"] == "2026-07-14T12:02:00+00:00"


def test_csv_report_quotes_unicode_newlines_and_neutralizes_formulas() -> None:
    item = _item()
    item.title = '=HYPERLINK("https://example.test","click")'
    item.artist = "+SUM(1,1)"
    item.reason = 'Line one, "quoted"\nLine two'
    row = build_report_row(_job(), item, outcome="partial")

    payload = csv_header_chunk() + csv_row_chunk(row)
    decoded = payload.decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(decoded)))
    by_name = dict(zip(parsed[0], parsed[1], strict=True))

    assert payload.startswith(b"\xef\xbb\xbf")
    assert by_name["title"].startswith("'=HYPERLINK")
    assert by_name["artist"] == "'+SUM(1,1)"
    assert by_name["reason"] == 'Line one, "quoted"\nLine two'
    assert by_name["source_metadata"] == json.dumps(
        row["source_metadata"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def test_empty_json_report_is_valid_and_contains_no_items() -> None:
    metadata = {
        "report_version": "1",
        "job_id": "job-123",
        "scope": "problems",
        "filters": {},
    }

    payload = json_report_prefix(metadata) + json_report_suffix()

    assert json.loads(payload) == {**metadata, "items": []}

