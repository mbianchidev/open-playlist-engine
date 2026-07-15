"""Stable migration report fields and streaming-safe encoders."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.migration_state import uri_keys
from app.db import models as orm

REPORT_VERSION = "1"
REPORT_FIELDS = (
    "report_version",
    "job_id",
    "job_status",
    "job_outcome",
    "job_error",
    "job_warnings",
    "job_created_at",
    "job_started_at",
    "job_completed_at",
    "detail_expires_at",
    "source_provider",
    "source_account_id",
    "target_provider",
    "target_account_id",
    "item_id",
    "source_playlist_id",
    "source_playlist_name",
    "target_playlist_id",
    "position",
    "title",
    "artist",
    "album",
    "duration_s",
    "release_year",
    "explicit",
    "isrc",
    "source_track_id",
    "source_item_id",
    "source_uri",
    "source_metadata",
    "target_uri",
    "target_id",
    "confidence",
    "status",
    "reason",
    "review_action",
    "review_original_status",
    "review_original_reason",
    "reviewed_at",
    "item_created_at",
    "item_updated_at",
)


def build_report_row(
    job: orm.MigrationJob,
    item: orm.JobItem,
    *,
    outcome: str,
) -> dict[str, Any]:
    metadata = item.source_metadata if isinstance(item.source_metadata, dict) else {}
    provider_uris = metadata.get("provider_uris")
    source_uri = (
        provider_uris.get(job.source_provider)
        if isinstance(provider_uris, dict)
        and isinstance(provider_uris.get(job.source_provider), str)
        else None
    )
    row = {
        "report_version": REPORT_VERSION,
        "job_id": job.id,
        "job_status": job.status,
        "job_outcome": outcome,
        "job_error": job.error,
        "job_warnings": job.warnings or [],
        "job_created_at": _iso(job.created_at),
        "job_started_at": _iso(job.started_at),
        "job_completed_at": _iso(job.completed_at),
        "detail_expires_at": _iso(job.details_expires_at),
        "source_provider": job.source_provider,
        "source_account_id": job.source_account_id,
        "target_provider": job.target_provider,
        "target_account_id": job.target_account_id,
        "item_id": item.id,
        "source_playlist_id": item.source_playlist_id,
        "source_playlist_name": item.source_playlist_name,
        "target_playlist_id": item.target_playlist_id,
        "position": item.position,
        "title": item.title,
        "artist": item.artist,
        "album": item.album,
        "duration_s": item.duration_s,
        "release_year": item.release_year,
        "explicit": item.explicit,
        "isrc": item.isrc,
        "source_track_id": _string_or_none(metadata.get("id")),
        "source_item_id": _string_or_none(metadata.get("source_item_id")),
        "source_uri": source_uri,
        "source_metadata": metadata,
        "target_uri": item.target_uri,
        "target_id": _provider_item_id(item.target_uri),
        "confidence": item.confidence,
        "status": item.status,
        "reason": item.reason,
        "review_action": item.review_action,
        "review_original_status": item.review_original_status,
        "review_original_reason": item.review_original_reason,
        "reviewed_at": _iso(item.reviewed_at),
        "item_created_at": _iso(item.created_at),
        "item_updated_at": _iso(item.updated_at),
    }
    return {field: row[field] for field in REPORT_FIELDS}


def csv_header_chunk() -> bytes:
    return b"\xef\xbb\xbf" + _csv_line(REPORT_FIELDS)


def csv_row_chunk(row: Mapping[str, Any]) -> bytes:
    return _csv_line([_csv_value(row.get(field)) for field in REPORT_FIELDS])


def json_report_prefix(metadata: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(
        dict(metadata),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    prefix = f"{encoded[:-1]},\"items\":[" if encoded != "{}" else '{"items":['
    return prefix.encode("utf-8")


def json_report_item_chunk(row: Mapping[str, Any], *, first: bool) -> bytes:
    prefix = "" if first else ","
    return (
        prefix
        + json.dumps(
            dict(row),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        )
    ).encode("utf-8")


def json_report_suffix() -> bytes:
    return b"]}"


def _csv_line(values: Any) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(values)
    return buffer.getvalue().encode("utf-8")


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    stripped = text.lstrip()
    if text.startswith(("\t", "\r")) or stripped.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _provider_item_id(uri: str | None) -> str | None:
    ids = sorted(key.removeprefix("id:") for key in uri_keys(uri) if key.startswith("id:"))
    return ids[0] if ids else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None

