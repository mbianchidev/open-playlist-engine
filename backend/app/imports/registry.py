from __future__ import annotations

import codecs
import csv
import re
from pathlib import PurePath
from typing import BinaryIO

from app.imports.models import ImportFormat, ImportIssueSeverity, ImportLimits, ImportParseResult
from app.imports.parsers import (
    PARSER_REGISTRY,
    ImportLimitExceeded,
    ParseContext,
    PlaylistImportError,
)

_EXTENSIONS = {
    ".txt": ImportFormat.TXT,
    ".csv": ImportFormat.CSV,
    ".m3u": ImportFormat.M3U,
    ".m3u8": ImportFormat.M3U8,
    ".pls": ImportFormat.PLS,
    ".wpl": ImportFormat.WPL,
    ".xspf": ImportFormat.XSPF,
    ".xml": ImportFormat.XML,
    ".json": ImportFormat.JSON,
}
_FILENAME_SEPARATORS = re.compile(r"[\\/]+")

__all__ = [
    "ImportLimitExceeded",
    "PlaylistImportError",
    "parse_playlist_file",
    "sanitize_filename",
]


def parse_playlist_file(
    source: BinaryIO,
    *,
    filename: str,
    limits: ImportLimits,
) -> ImportParseResult:
    safe_filename = sanitize_filename(filename)
    file_size = _file_size(source)
    if file_size > limits.max_upload_bytes:
        raise ImportLimitExceeded(
            f"Upload is {file_size} bytes; the configured limit is "
            f"{limits.max_upload_bytes} bytes.",
            code="upload_size_limit",
        )
    if file_size == 0:
        extension_format = _format_from_filename(safe_filename)
        raise PlaylistImportError(
            "The file contains no playlist tracks that can be previewed.",
            code="empty_import",
            format=extension_format,
        )

    detected_format, extension_format = _detect_format(source, safe_filename)
    context = ParseContext(
        format=detected_format,
        filename=safe_filename,
        file_size=file_size,
        limits=limits,
    )
    if extension_format and extension_format is not detected_format:
        context.issue(
            ImportIssueSeverity.WARNING,
            "extension_mismatch",
            f"File extension suggests {extension_format.value}, but content was detected as "
            f"{detected_format.value}. The detected content format was used.",
        )
    parser = PARSER_REGISTRY[detected_format]
    source.seek(0)
    try:
        parser(source, context)
    except PlaylistImportError:
        raise
    except UnicodeError as exc:
        raise PlaylistImportError(
            "The file encoding is invalid or truncated.",
            code="invalid_encoding",
            format=detected_format,
        ) from exc
    except csv.Error as exc:
        raise PlaylistImportError(
            f"CSV could not be parsed: {exc}.",
            code="invalid_document",
            format=detected_format,
        ) from exc
    return context.result()


def sanitize_filename(filename: str) -> str:
    value = filename.replace("\x00", "").strip()
    value = _FILENAME_SEPARATORS.split(value)[-1]
    return value[:255] or "playlist"


def _file_size(source: BinaryIO) -> int:
    source.seek(0, 2)
    size = source.tell()
    source.seek(0)
    return size


def _format_from_filename(filename: str) -> ImportFormat | None:
    return _EXTENSIONS.get(PurePath(filename).suffix.lower())


def _detect_format(
    source: BinaryIO, filename: str
) -> tuple[ImportFormat, ImportFormat | None]:
    extension_format = _format_from_filename(filename)
    source.seek(0)
    head = source.read(64 * 1024)
    source.seek(0)
    probe = _decode_probe(head).lstrip()
    lower = probe.lower()

    detected: ImportFormat | None = None
    if lower.startswith(("{", "[")) and not lower.startswith("[playlist]"):
        detected = ImportFormat.JSON
    elif lower.startswith("[playlist]"):
        detected = ImportFormat.PLS
    elif lower.startswith("#extm3u"):
        detected = (
            ImportFormat.M3U8 if extension_format is ImportFormat.M3U8 else ImportFormat.M3U
        )
    elif lower.startswith("<?xml") or lower.startswith("<"):
        if "xspf.org/ns/" in lower:
            detected = ImportFormat.XSPF
        elif "<smil" in lower:
            detected = ImportFormat.WPL
        else:
            detected = extension_format if extension_format in {
                ImportFormat.WPL,
                ImportFormat.XSPF,
            } else ImportFormat.XML
    elif extension_format is ImportFormat.CSV or _looks_like_csv_header(probe):
        detected = ImportFormat.CSV
    elif extension_format:
        detected = extension_format

    if detected is None:
        supported = ", ".join(format.value for format in ImportFormat)
        raise PlaylistImportError(
            f"Unsupported playlist format. Supported formats: {supported}.",
            code="unsupported_format",
        )
    return detected, extension_format


def _decode_probe(payload: bytes) -> str:
    if payload.startswith(codecs.BOM_UTF8):
        return payload.decode("utf-8-sig", errors="replace")
    if payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return payload.decode("utf-16", errors="replace")
    if payload.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return payload.decode("utf-32", errors="replace")
    return payload.decode("utf-8", errors="replace")


def _looks_like_csv_header(value: str) -> bool:
    first_line = value.splitlines()[0].lower() if value.splitlines() else ""
    return any(delimiter in first_line for delimiter in (",", ";", "\t", "|")) and any(
        name in first_line for name in ("title", "track", "song")
    )
