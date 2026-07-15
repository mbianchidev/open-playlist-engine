from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import TextIO
from xml.sax.saxutils import escape, quoteattr

from app.core.models import Playlist, Track
from app.exports.models import (
    OPEN_PLAYLIST_BUNDLE_SCHEMA,
    ExportFormat,
    ExportManifest,
    ExportWarning,
    OpenPlaylistBundle,
)

XSPF_NAMESPACE = "http://xspf.org/ns/0/"
OPE_XSPF_NAMESPACE = "https://openplaylistengine.dev/ns/export/v1"
OPE_XSPF_APPLICATION = "https://openplaylistengine.dev/schemas/export/xspf-v1"
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_XML_ILLEGAL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]"
)
_TABULAR_COLUMNS = [
    "schema_version",
    "playlist_id",
    "playlist_name",
    "playlist_description",
    "playlist_kind",
    "playlist_owner_id",
    "playlist_artwork_uri",
    "playlist_created_at",
    "playlist_updated_at",
    "order",
    "source_position",
    "track_id",
    "source_item_id",
    "title",
    "artist",
    "album",
    "duration_seconds",
    "isrc",
    "source_uri",
    "artwork_uri",
    "added_at",
    "media_type",
    "unsupported_reason",
]


@dataclass(frozen=True)
class FormatSpec:
    extension: str
    media_type: str


FORMAT_SPECS = {
    ExportFormat.CSV: FormatSpec("csv", "text/csv; charset=utf-8"),
    ExportFormat.TXT: FormatSpec("txt", "text/plain; charset=utf-8"),
    ExportFormat.M3U8: FormatSpec("m3u8", "application/vnd.apple.mpegurl; charset=utf-8"),
    ExportFormat.XSPF: FormatSpec("xspf", "application/xspf+xml; charset=utf-8"),
    ExportFormat.JSON: FormatSpec(
        "json",
        "application/vnd.open-playlist+json; charset=utf-8",
    ),
}


def safe_filename(value: str, *, fallback: str = "playlist", max_length: int = 96) -> str:
    base = _filename_base(value)
    fallback_base = _filename_base(fallback) or "playlist"
    if not base:
        base = fallback_base
    if base.upper() in _WINDOWS_RESERVED:
        base = f"playlist-{base}"
    base = base[:max_length].rstrip("-")
    return base or fallback_base[:max_length] or "playlist"


class FilenameAllocator:
    def __init__(self) -> None:
        self._used: set[str] = set()

    def allocate(self, value: str, *, extension: str, fallback: str = "playlist") -> str:
        if not re.fullmatch(r"[a-z0-9]+", extension):
            raise ValueError(f"unsafe filename extension: {extension!r}")
        base = safe_filename(value, fallback=fallback)
        candidate = f"{base}.{extension}"
        sequence = 2
        while candidate.casefold() in self._used:
            suffix = f"-{sequence}"
            trimmed = base[: 96 - len(suffix)].rstrip("-")
            candidate = f"{trimmed or 'playlist'}{suffix}.{extension}"
            sequence += 1
        self._used.add(candidate.casefold())
        return candidate


class JsonBundleWriter:
    def __init__(self, stream: TextIO, *, source_provider: str) -> None:
        self._stream = stream
        self._first_playlist = True
        self._finished = False
        self._stream.write('{"$schema":')
        _dump_json(OPEN_PLAYLIST_BUNDLE_SCHEMA, self._stream)
        self._stream.write(',"schema_version":1,"source_provider":')
        _dump_json(source_provider, self._stream)
        self._stream.write(',"playlists":[')

    def write_playlist(self, playlist: Playlist) -> None:
        if self._finished:
            raise RuntimeError("JSON bundle is already finished")
        if not self._first_playlist:
            self._stream.write(",")
        self._first_playlist = False
        metadata = playlist.model_dump(mode="json", exclude={"tracks"})
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        self._stream.write(encoded[:-1])
        self._stream.write(',"tracks":[')
        for index, track in enumerate(playlist.tracks):
            if index:
                self._stream.write(",")
            _dump_json(track.model_dump(mode="json"), self._stream)
        self._stream.write("]}")

    def finish(self, warnings: list[ExportWarning]) -> None:
        if self._finished:
            raise RuntimeError("JSON bundle is already finished")
        self._finished = True
        self._stream.write('],"warnings":')
        _dump_json([warning.model_dump(mode="json") for warning in warnings], self._stream)
        self._stream.write("}\n")


def parse_open_playlist_bundle(payload: str | bytes) -> OpenPlaylistBundle:
    return OpenPlaylistBundle.model_validate_json(payload)


def write_playlist(
    stream: TextIO,
    export_format: ExportFormat,
    playlist: Playlist,
    *,
    source_provider: str,
    warnings: list[ExportWarning] | None = None,
) -> None:
    if export_format is ExportFormat.CSV:
        _write_tabular(stream, playlist, source_provider=source_provider, delimiter=",", bom=True)
        return
    if export_format is ExportFormat.TXT:
        stream.write("# Open Playlist TXT v1\n# Encoding: UTF-8\n")
        _write_tabular(stream, playlist, source_provider=source_provider, delimiter="\t", bom=False)
        return
    if export_format is ExportFormat.M3U8:
        _write_m3u8(stream, playlist, source_provider=source_provider)
        return
    if export_format is ExportFormat.XSPF:
        _write_xspf(stream, playlist, source_provider=source_provider)
        return
    if export_format is ExportFormat.JSON:
        writer = JsonBundleWriter(stream, source_provider=source_provider)
        writer.write_playlist(playlist)
        writer.finish(warnings or [])
        return
    raise ValueError(f"unsupported export format: {export_format}")


def write_manifest(stream: TextIO, manifest: ExportManifest) -> None:
    json.dump(
        manifest.model_dump(mode="json", by_alias=True),
        stream,
        ensure_ascii=False,
        indent=2,
    )
    stream.write("\n")


def source_uri(track: Track, source_provider: str) -> str | None:
    preferred = track.provider_uris.get(source_provider)
    if preferred:
        return preferred
    for provider in sorted(track.provider_uris):
        uri = track.provider_uris[provider]
        if uri:
            return uri
    return None


def portable_location(track: Track, source_provider: str) -> str | None:
    uri = source_uri(track, source_provider)
    if not uri:
        return None
    if uri.startswith("spotify:track:"):
        return f"https://open.spotify.com/track/{uri.rsplit(':', 1)[-1]}"
    if uri.startswith("tidal:track:"):
        return f"https://tidal.com/browse/track/{uri.rsplit(':', 1)[-1]}"
    return uri


def _filename_base(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_value = re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_value)
    ascii_value = re.sub(r"[-_]+", "-", ascii_value)
    return ascii_value.strip("-")


def _write_tabular(
    stream: TextIO,
    playlist: Playlist,
    *,
    source_provider: str,
    delimiter: str,
    bom: bool,
) -> None:
    if bom:
        stream.write("\ufeff")
    writer = csv.DictWriter(
        stream,
        fieldnames=_TABULAR_COLUMNS,
        delimiter=delimiter,
        lineterminator="\n",
    )
    writer.writeheader()
    rows = (
        _tabular_rows(playlist, source_provider=source_provider)
        if playlist.tracks
        else [_tabular_row(playlist, None, order=None, source_provider=source_provider)]
    )
    for row in rows:
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})


def _tabular_rows(playlist: Playlist, *, source_provider: str):
    for order, track in enumerate(playlist.tracks, start=1):
        yield _tabular_row(
            playlist,
            track,
            order=order,
            source_provider=source_provider,
        )


def _tabular_row(
    playlist: Playlist,
    track: Track | None,
    *,
    order: int | None,
    source_provider: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "playlist_id": playlist.id or "",
        "playlist_name": playlist.name,
        "playlist_description": playlist.description or "",
        "playlist_kind": playlist.kind.value,
        "playlist_owner_id": playlist.owner_id or "",
        "playlist_artwork_uri": playlist.photo or "",
        "playlist_created_at": _iso(playlist.created_at),
        "playlist_updated_at": _iso(playlist.updated_at),
        "order": order if order is not None else "",
        "source_position": track.position if track and track.position is not None else "",
        "track_id": track.id if track and track.id else "",
        "source_item_id": track.source_item_id if track and track.source_item_id else "",
        "title": track.title if track else "",
        "artist": track.artist if track else "",
        "album": track.album if track and track.album else "",
        "duration_seconds": (
            track.duration_s if track and track.duration_s is not None else ""
        ),
        "isrc": track.isrc if track and track.isrc else "",
        "source_uri": source_uri(track, source_provider) if track else "",
        "artwork_uri": track.artwork_uri if track and track.artwork_uri else "",
        "added_at": _iso(track.added_at) if track else "",
        "media_type": track.media_type.value if track else "",
        "unsupported_reason": (
            track.unsupported_reason if track and track.unsupported_reason else ""
        ),
    }


def _write_m3u8(stream: TextIO, playlist: Playlist, *, source_provider: str) -> None:
    stream.write("#EXTM3U\n")
    stream.write("#EXTENC:UTF-8\n")
    stream.write("#OPE-SCHEMA:open-playlist-m3u-v1\n")
    stream.write(f"#PLAYLIST:{_single_line(playlist.name)}\n")
    if playlist.id:
        stream.write(f"#OPE-PLAYLIST-ID:{_single_line(playlist.id)}\n")
    if not playlist.tracks:
        stream.write("#OPE-WARNING:empty_playlist\n")
    for order, track in enumerate(playlist.tracks, start=1):
        duration = track.duration_s if track.duration_s is not None else -1
        label = f"{track.artist} - {track.title}".strip(" -")
        stream.write(f"#EXTINF:{duration},{_single_line(label)}\n")
        stream.write(f"#OPE-ORDER:{order}\n")
        if track.artist:
            stream.write(f"#EXTART:{_single_line(track.artist)}\n")
        if track.album:
            stream.write(f"#EXTALB:{_single_line(track.album)}\n")
        if track.isrc:
            stream.write(f"#OPE-ISRC:{_single_line(track.isrc)}\n")
        stream.write(f"#OPE-MEDIA-TYPE:{track.media_type.value}\n")
        raw_uri = source_uri(track, source_provider)
        if raw_uri:
            stream.write(f"#OPE-SOURCE-URI:{_single_line(raw_uri)}\n")
        if track.added_at:
            stream.write(f"#OPE-ADDED-AT:{_iso(track.added_at)}\n")
        if track.artwork_uri:
            stream.write(f"#EXTIMG:{_single_line(track.artwork_uri)}\n")
        if track.unsupported_reason:
            stream.write(f"#OPE-UNSUPPORTED:{_single_line(track.unsupported_reason)}\n")
        location = portable_location(track, source_provider)
        stream.write(f"{_single_line(location)}\n" if location else "#OPE-MISSING-URI\n")


def _write_xspf(stream: TextIO, playlist: Playlist, *, source_provider: str) -> None:
    stream.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    stream.write(
        f'<playlist version="1" xmlns={quoteattr(XSPF_NAMESPACE)} '
        f'xmlns:ope={quoteattr(OPE_XSPF_NAMESPACE)}>\n'
    )
    _xml_element(stream, "title", playlist.name, indent="  ")
    if playlist.description:
        _xml_element(stream, "annotation", playlist.description, indent="  ")
    if playlist.photo:
        _xml_element(stream, "image", playlist.photo, indent="  ")
    stream.write(
        f"  <extension application={quoteattr(OPE_XSPF_APPLICATION)}>\n"
    )
    _xml_element(stream, "ope:schemaVersion", "1", indent="    ")
    if playlist.id:
        _xml_element(stream, "ope:playlistId", playlist.id, indent="    ")
    _xml_element(stream, "ope:playlistKind", playlist.kind.value, indent="    ")
    stream.write("  </extension>\n")
    stream.write("  <trackList>\n")
    for order, track in enumerate(playlist.tracks, start=1):
        stream.write("    <track>\n")
        location = portable_location(track, source_provider)
        if location:
            _xml_element(stream, "location", location, indent="      ")
        if track.isrc:
            _xml_element(stream, "identifier", f"urn:isrc:{track.isrc}", indent="      ")
        _xml_element(stream, "title", track.title, indent="      ")
        _xml_element(stream, "creator", track.artist, indent="      ")
        if track.album:
            _xml_element(stream, "album", track.album, indent="      ")
        if track.duration_s is not None:
            _xml_element(stream, "duration", str(track.duration_s * 1000), indent="      ")
        if track.artwork_uri:
            _xml_element(stream, "image", track.artwork_uri, indent="      ")
        stream.write(
            f"      <extension application={quoteattr(OPE_XSPF_APPLICATION)}>\n"
        )
        _xml_element(stream, "ope:order", str(order), indent="        ")
        if track.position is not None:
            _xml_element(stream, "ope:sourcePosition", str(track.position), indent="        ")
        raw_uri = source_uri(track, source_provider)
        if raw_uri:
            _xml_element(stream, "ope:sourceUri", raw_uri, indent="        ")
        if track.added_at:
            _xml_element(stream, "ope:addedAt", _iso(track.added_at), indent="        ")
        _xml_element(stream, "ope:mediaType", track.media_type.value, indent="        ")
        if track.unsupported_reason:
            _xml_element(
                stream,
                "ope:unsupportedReason",
                track.unsupported_reason,
                indent="        ",
            )
        stream.write("      </extension>\n")
        stream.write("    </track>\n")
    stream.write("  </trackList>\n")
    stream.write("</playlist>\n")


def _spreadsheet_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    if value.startswith(("\t", "\r")) or stripped.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _iso(value: date | datetime | None) -> str:
    if value is None:
        return ""
    encoded = value.isoformat()
    return encoded.replace("+00:00", "Z")


def _single_line(value: str | None) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ")


def _xml_element(stream: TextIO, tag: str, value: str, *, indent: str) -> None:
    text = escape(_xml_text(value))
    stream.write(f"{indent}<{tag}>{text}</{tag}>\n")


def _xml_text(value: str) -> str:
    return _XML_ILLEGAL.sub("", value)


def _dump_json(value: object, stream: TextIO) -> None:
    json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
