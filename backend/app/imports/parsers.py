from __future__ import annotations

import codecs
import csv
import json
import math
import re
import tempfile
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import TextIOWrapper
from pathlib import PurePath
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse
from xml.etree.ElementTree import ParseError

import ijson
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.core.models import MediaType, Playlist, Track
from app.imports.models import (
    ImportFormat,
    ImportIssue,
    ImportIssueSeverity,
    ImportLimits,
    ImportParseResult,
)

_KEY_NOISE = re.compile(r"[^a-z0-9]+")
_DRIVE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")
_PLS_ENTRY = re.compile(r"^(file|title|length)(\d+)$", re.IGNORECASE)
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_XML_ENCODING = re.compile(br"encoding=[\"']([^\"']+)[\"']", re.IGNORECASE)

_TITLE_ALIASES = (
    "title",
    "track",
    "track_title",
    "tracktitle",
    "song",
    "song_title",
    "name",
)
_ARTIST_ALIASES = (
    "artist",
    "artists",
    "artist_name",
    "track_artist",
    "trackartist",
    "creator",
    "performer",
)
_ALBUM_ALIASES = ("album", "album_name", "album_title", "albumtitle", "release")
_DURATION_ALIASES = (
    "duration_s",
    "duration_seconds",
    "duration",
    "length",
    "duration_ms",
    "length_ms",
)
_ISRC_ALIASES = ("isrc", "recording_isrc")
_URI_ALIASES = (
    "uri",
    "url",
    "location",
    "src",
    "path",
    "file",
    "provider_uri",
    "track_uri",
    "link",
)
_PLAYLIST_ALIASES = ("playlist", "playlist_name", "playlistname", "list")
_ID_ALIASES = ("id", "track_id", "trackid", "identifier")


class PlaylistImportError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        format: ImportFormat | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.format = format


class ImportLimitExceeded(PlaylistImportError):
    pass


@dataclass
class ParseContext:
    format: ImportFormat
    filename: str
    file_size: int
    limits: ImportLimits
    encoding: str | None = None
    playlists: list[Playlist] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)
    track_count: int = 0
    malformed_count: int = 0
    unsupported_count: int = 0
    duplicate_count: int = 0
    _parsed_track_count: int = 0
    _parsed_playlist_count: int = 0
    _issue_overflow_reported: bool = False

    def issue(
        self,
        severity: ImportIssueSeverity,
        code: str,
        message: str,
        *,
        line_or_item: int | str | None = None,
        playlist_name: str | None = None,
        raw_excerpt: str | None = None,
    ) -> None:
        if len(self.issues) < self.limits.max_issues:
            self.issues.append(
                ImportIssue(
                    severity=severity,
                    code=code,
                    message=message,
                    line_or_item=line_or_item,
                    playlist_name=playlist_name,
                    raw_excerpt=_excerpt(raw_excerpt),
                )
            )
            return
        if self._issue_overflow_reported:
            return
        self._issue_overflow_reported = True
        self.issues.append(
            ImportIssue(
                severity=ImportIssueSeverity.WARNING,
                code="issue_limit",
                message=(
                    f"Only the first {self.limits.max_issues} parse issues are shown. "
                    "Additional malformed or lossy entries were counted but omitted."
                ),
            )
        )

    def malformed(
        self,
        code: str,
        message: str,
        *,
        line_or_item: int | str | None = None,
        playlist_name: str | None = None,
        raw_excerpt: str | None = None,
    ) -> None:
        self.malformed_count += 1
        self.issue(
            ImportIssueSeverity.ERROR,
            code,
            message,
            line_or_item=line_or_item,
            playlist_name=playlist_name,
            raw_excerpt=raw_excerpt,
        )

    def add_playlist(self, name: str | None, tracks: list[Track]) -> None:
        self.reserve_playlist()
        if not tracks:
            self.malformed(
                "empty_playlist",
                "A playlist contained no usable or reportable track entries.",
                playlist_name=_clean(name),
            )
            return
        if self.track_count + len(tracks) > self.limits.max_tracks:
            raise ImportLimitExceeded(
                f"Track limit exceeded; this import allows at most "
                f"{self.limits.max_tracks} tracks.",
                code="track_limit",
                format=self.format,
            )
        playlist_name = _clean(name) or _default_playlist_name(self.filename)
        self.playlists.append(Playlist(name=playlist_name, tracks=tracks))
        self.track_count += len(tracks)
        self.unsupported_count += sum(not track.is_migratable for track in tracks)

    def reserve_playlist(self) -> None:
        if self._parsed_playlist_count >= self.limits.max_playlists:
            raise ImportLimitExceeded(
                f"Playlist limit exceeded; this import allows at most "
                f"{self.limits.max_playlists} playlists.",
                code="playlist_limit",
                format=self.format,
            )
        self._parsed_playlist_count += 1

    def reserve_track(self) -> None:
        if self._parsed_track_count >= self.limits.max_tracks:
            raise ImportLimitExceeded(
                f"Track limit exceeded; this import allows at most "
                f"{self.limits.max_tracks} tracks.",
                code="track_limit",
                format=self.format,
            )
        self._parsed_track_count += 1

    def result(self) -> ImportParseResult:
        if not self.playlists or self.track_count == 0:
            raise PlaylistImportError(
                "The file contains no playlist tracks that can be previewed.",
                code="empty_import",
                format=self.format,
            )
        finalized: list[Playlist] = []
        for playlist in self.playlists:
            playlist_id = f"local:{uuid.uuid4()}"
            seen: dict[str, int] = {}
            tracks: list[Track] = []
            for position, raw_track in enumerate(playlist.tracks):
                signature = _duplicate_signature(raw_track)
                metadata = dict(raw_track.metadata)
                if signature and signature in seen:
                    duplicate_position = seen[signature]
                    metadata["import_duplicate_of_position"] = duplicate_position
                    self.duplicate_count += 1
                    self.issue(
                        ImportIssueSeverity.WARNING,
                        "duplicate_track",
                        f"Duplicate of track {duplicate_position + 1}; ordering was preserved.",
                        line_or_item=position + 1,
                        playlist_name=playlist.name,
                    )
                elif signature:
                    seen[signature] = position
                source_item_id = f"{playlist_id}:{position}"
                tracks.append(
                    raw_track.model_copy(
                        update={
                            "id": source_item_id,
                            "source_item_id": source_item_id,
                            "position": position,
                            "metadata": metadata,
                        }
                    )
                )
            finalized.append(playlist.model_copy(update={"id": playlist_id, "tracks": tracks}))
        return ImportParseResult(
            detected_format=self.format,
            encoding=self.encoding,
            file_size=self.file_size,
            playlists=finalized,
            issues=self.issues,
            playlist_count=len(finalized),
            track_count=self.track_count,
            duplicate_count=self.duplicate_count,
            malformed_count=self.malformed_count,
            unsupported_count=self.unsupported_count,
        )


Parser = Callable[[BinaryIO, ParseContext], None]


def parse_txt(source: BinaryIO, context: ParseContext) -> None:
    groups: OrderedDict[str, list[Track]] = OrderedDict()
    current_name = _default_playlist_name(context.filename)
    with _text_reader(source, context) as text:
        for line_number, raw_line in enumerate(text, start=1):
            line = raw_line.strip()
            if not line:
                continue
            if line.upper().startswith("#PLAYLIST:"):
                current_name = _clean(line.split(":", 1)[1]) or current_name
                continue
            if line.startswith("#"):
                continue
            mapping: dict[str, Any]
            if "\t" in line:
                values = [value.strip() for value in line.split("\t")]
                keys = ("title", "artist", "album", "duration", "isrc", "uri", "playlist")
                mapping = dict(zip(keys, values, strict=False))
                current_name = _clean(mapping.get("playlist")) or current_name
            elif _looks_like_location(line):
                mapping = {"uri": line}
            else:
                artist, title = _split_artist_title(line)
                mapping = {"title": title or line, "artist": artist}
            track = _track_from_mapping(
                context,
                mapping,
                line_or_item=line_number,
                playlist_name=current_name,
                raw_excerpt=line,
            )
            groups.setdefault(current_name, []).append(track)
    _add_groups(context, groups)


def parse_csv(source: BinaryIO, context: ParseContext) -> None:
    groups: OrderedDict[str, list[Track]] = OrderedDict()
    with _text_reader(source, context, newline="") as text:
        sample = text.read(8192)
        text.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(text, dialect=dialect)
        normalized_headers = {
            _normalize_key(header) for header in reader.fieldnames or [] if header
        }
        if not normalized_headers.intersection({_normalize_key(value) for value in _TITLE_ALIASES}):
            raise PlaylistImportError(
                "CSV must include a title column. See the documented canonical CSV schema.",
                code="missing_columns",
                format=context.format,
            )
        for row_number, row in enumerate(reader, start=2):
            normalized = _normalize_mapping(row)
            playlist_name = _clean(_first(normalized, _PLAYLIST_ALIASES)) or _default_playlist_name(
                context.filename
            )
            track = _track_from_mapping(
                context,
                row,
                line_or_item=row_number,
                playlist_name=playlist_name,
                raw_excerpt=" | ".join(str(value or "") for value in row.values()),
            )
            groups.setdefault(playlist_name, []).append(track)
    _add_groups(context, groups)


def parse_m3u(source: BinaryIO, context: ParseContext) -> None:
    groups: OrderedDict[str, list[Track]] = OrderedDict()
    current_name = _default_playlist_name(context.filename)
    pending: dict[str, Any] | None = None
    pending_line: int | None = None
    with _text_reader(source, context) as text:
        for line_number, raw_line in enumerate(text, start=1):
            line = raw_line.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("#PLAYLIST:"):
                current_name = _clean(line.split(":", 1)[1]) or current_name
                continue
            if upper.startswith("#EXTINF:"):
                payload = line.split(":", 1)[1]
                duration_raw, separator, display = payload.partition(",")
                pending = {"duration": duration_raw}
                if separator:
                    artist, title = _split_artist_title(display)
                    pending.update({"title": title or display, "artist": artist})
                else:
                    context.malformed(
                        "invalid_extinf",
                        "M3U EXTINF metadata must contain a comma before the display title.",
                        line_or_item=line_number,
                        playlist_name=current_name,
                        raw_excerpt=line,
                    )
                pending_line = line_number
                continue
            if line.startswith("#"):
                continue
            mapping = dict(pending or {})
            mapping["uri"] = line
            track = _track_from_mapping(
                context,
                mapping,
                line_or_item=line_number,
                playlist_name=current_name,
                raw_excerpt=line,
            )
            groups.setdefault(current_name, []).append(track)
            pending = None
            pending_line = None
    if pending is not None:
        context.malformed(
            "missing_location",
            "M3U metadata was not followed by a track location.",
            line_or_item=pending_line,
            playlist_name=current_name,
        )
    _add_groups(context, groups)


def parse_pls(source: BinaryIO, context: ParseContext) -> None:
    entries: dict[int, dict[str, str]] = {}
    playlist_name = _default_playlist_name(context.filename)
    expected_entries: int | None = None
    with _text_reader(source, context) as text:
        for line_number, raw_line in enumerate(text, start=1):
            line = raw_line.strip()
            if not line or line.startswith((";", "#", "[")):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                context.malformed(
                    "invalid_pls_line",
                    "PLS entries must use key=value syntax.",
                    line_or_item=line_number,
                    playlist_name=playlist_name,
                    raw_excerpt=line,
                )
                continue
            normalized_key = _normalize_key(key)
            if normalized_key == "numberofentries":
                expected_entries = _int_or_none(value)
                continue
            if normalized_key == "title":
                playlist_name = _clean(value) or playlist_name
                continue
            match = _PLS_ENTRY.match(normalized_key)
            if not match:
                continue
            field_name, index_raw = match.groups()
            entries.setdefault(int(index_raw), {})[field_name.lower()] = value.strip()

    tracks: list[Track] = []
    for index in sorted(entries):
        entry = entries[index]
        display = entry.get("title")
        artist, title = _split_artist_title(display or "")
        mapping = {
            "uri": entry.get("file"),
            "title": title or display,
            "artist": artist,
            "duration": entry.get("length"),
        }
        tracks.append(
            _track_from_mapping(
                context,
                mapping,
                line_or_item=index,
                playlist_name=playlist_name,
                raw_excerpt=display or entry.get("file"),
            )
        )
    if expected_entries is not None and expected_entries != len(entries):
        context.issue(
            ImportIssueSeverity.WARNING,
            "entry_count_mismatch",
            f"PLS declares {expected_entries} entries but contains {len(entries)}.",
            playlist_name=playlist_name,
        )
    context.add_playlist(playlist_name, tracks)


def parse_wpl(source: BinaryIO, context: ParseContext) -> None:
    root = _parse_xml_root(source, context)
    playlist_name = _first_element_text(root, "title") or _default_playlist_name(
        context.filename
    )
    tracks: list[Track] = []
    for item_number, element in enumerate(_elements_named(root, "media"), start=1):
        attrs = dict(element.attrib)
        attrs["duration_ms"] = attrs.pop("duration", None)
        tracks.append(
            _track_from_mapping(
                context,
                attrs,
                line_or_item=item_number,
                playlist_name=playlist_name,
                raw_excerpt=attrs.get("src"),
            )
        )
    context.add_playlist(playlist_name, tracks)


def parse_xspf(source: BinaryIO, context: ParseContext) -> None:
    root = _parse_xml_root(source, context)
    playlist_name = _direct_child_text(root, "title") or _default_playlist_name(
        context.filename
    )
    tracks: list[Track] = []
    for item_number, element in enumerate(_elements_named(root, "track"), start=1):
        mapping = {_local_name(child.tag): _clean(child.text) for child in list(element)}
        mapping["duration_ms"] = mapping.pop("duration", None)
        identifier = _clean(mapping.get("identifier"))
        if identifier and _isrc(identifier):
            mapping["isrc"] = identifier
        tracks.append(
            _track_from_mapping(
                context,
                mapping,
                line_or_item=item_number,
                playlist_name=playlist_name,
                raw_excerpt=mapping.get("location"),
            )
        )
    context.add_playlist(playlist_name, tracks)


def parse_xml(source: BinaryIO, context: ParseContext) -> None:
    root = _parse_xml_root(source, context)
    playlist_elements = (
        [root] if _local_name(root.tag) == "playlist" else list(_elements_named(root, "playlist"))
    )
    if not playlist_elements:
        playlist_elements = [root]
    for playlist_number, playlist_element in enumerate(playlist_elements, start=1):
        playlist_name = (
            _clean(playlist_element.attrib.get("name"))
            or _direct_child_text(playlist_element, "name")
            or _direct_child_text(playlist_element, "title")
            or f"{_default_playlist_name(context.filename)} {playlist_number}"
        )
        tracks: list[Track] = []
        for item_number, element in enumerate(
            _elements_named(playlist_element, "track"), start=1
        ):
            mapping = dict(element.attrib)
            for child in list(element):
                if len(child) == 0:
                    mapping.setdefault(_local_name(child.tag), _clean(child.text))
            tracks.append(
                _track_from_mapping(
                    context,
                    mapping,
                    line_or_item=item_number,
                    playlist_name=playlist_name,
                    raw_excerpt=str(mapping.get("uri") or mapping.get("location") or ""),
                )
            )
        context.add_playlist(playlist_name, tracks)


def parse_json(source: BinaryIO, context: ParseContext) -> None:
    _validate_json_limits(source, context)
    with _text_reader(source, context) as text:
        try:
            payload = json.load(text)
        except json.JSONDecodeError as exc:
            raise PlaylistImportError(
                f"JSON could not be parsed at line {exc.lineno}, column {exc.colno}.",
                code="invalid_document",
                format=context.format,
            ) from exc

    playlists = _json_playlists(payload, context.filename)
    for playlist_number, (playlist_name, raw_tracks) in enumerate(playlists, start=1):
        tracks: list[Track] = []
        for item_number, raw_track in enumerate(raw_tracks, start=1):
            if isinstance(raw_track, Mapping):
                nested = raw_track.get("track")
                mapping = nested if isinstance(nested, Mapping) else raw_track
            else:
                mapping = {"title": str(raw_track)}
            tracks.append(
                _track_from_mapping(
                    context,
                    mapping,
                    line_or_item=item_number,
                    playlist_name=playlist_name,
                    raw_excerpt=json.dumps(raw_track, ensure_ascii=False)[:160],
                )
            )
        context.add_playlist(
            playlist_name or f"{_default_playlist_name(context.filename)} {playlist_number}",
            tracks,
        )


PARSER_REGISTRY: dict[ImportFormat, Parser] = {
    ImportFormat.TXT: parse_txt,
    ImportFormat.CSV: parse_csv,
    ImportFormat.M3U: parse_m3u,
    ImportFormat.M3U8: parse_m3u,
    ImportFormat.PLS: parse_pls,
    ImportFormat.WPL: parse_wpl,
    ImportFormat.XSPF: parse_xspf,
    ImportFormat.XML: parse_xml,
    ImportFormat.JSON: parse_json,
}


def _track_from_mapping(
    context: ParseContext,
    mapping: Mapping[str, Any],
    *,
    line_or_item: int | str,
    playlist_name: str,
    raw_excerpt: str | None,
) -> Track:
    context.reserve_track()
    normalized = _normalize_mapping(mapping)
    title = _clean(_first(normalized, _TITLE_ALIASES))
    artist = _clean(_first(normalized, _ARTIST_ALIASES))
    uri = _clean(_first(normalized, _URI_ALIASES))
    if title and not artist:
        parsed_artist, parsed_title = _split_artist_title(title)
        if parsed_artist and parsed_title:
            artist, title = parsed_artist, parsed_title
    original_id = _clean(_first(normalized, _ID_ALIASES))
    isrc = _clean(_first(normalized, _ISRC_ALIASES))
    if not isrc and original_id and _isrc(original_id):
        isrc = original_id
    if isrc:
        isrc = re.sub(r"[^A-Za-z0-9]", "", isrc).upper()
    duration_key, duration_value = _first_with_key(normalized, _DURATION_ALIASES)
    duration_s = _parse_duration(duration_value, milliseconds=duration_key.endswith("_ms"))
    if duration_value not in (None, "") and duration_s is None:
        context.malformed(
            "invalid_duration",
            f"Duration value '{duration_value}' is invalid and was ignored.",
            line_or_item=line_or_item,
            playlist_name=playlist_name,
            raw_excerpt=raw_excerpt,
        )
    is_local = _looks_like_local_location(uri)
    missing = [field for field, value in (("title", title), ("artist", artist)) if not value]
    unsupported_reason: str | None = None
    media_type = MediaType.TRACK
    if is_local:
        media_type = MediaType.LOCAL_FILE
        unsupported_reason = "Local audio-file entries are not uploaded or migrated."
        context.issue(
            ImportIssueSeverity.WARNING,
            "unsupported_local_file",
            unsupported_reason,
            line_or_item=line_or_item,
            playlist_name=playlist_name,
            raw_excerpt=raw_excerpt,
        )
    elif missing:
        media_type = MediaType.UNKNOWN
        unsupported_reason = (
            "Track metadata is incomplete; both title and artist are required for matching."
        )
    if missing:
        context.malformed(
            f"missing_{'_and_'.join(missing)}",
            f"Track entry is missing {', '.join(missing)}.",
            line_or_item=line_or_item,
            playlist_name=playlist_name,
            raw_excerpt=raw_excerpt,
        )
    title = title or _location_title(uri) or "Unknown track"
    artist = artist or "Unknown artist"
    provider = _provider_for_uri(uri)
    provider_uris = {provider: uri} if provider and uri else {}
    metadata: dict[str, object] = {
        "import_format": context.format.value,
        "import_location": line_or_item,
    }
    serializable_fields = {
        key: value
        for key, value in normalized.items()
        if value is not None and isinstance(value, (str, int, float, bool))
    }
    if serializable_fields:
        metadata["import_fields"] = serializable_fields
    if uri:
        metadata["source_uri"] = uri
    if original_id:
        metadata["original_id"] = original_id
    return Track(
        title=title,
        artist=artist,
        album=_clean(_first(normalized, _ALBUM_ALIASES)),
        duration_s=duration_s,
        isrc=isrc,
        provider_uris=provider_uris,
        metadata=metadata,
        media_type=media_type,
        is_local=is_local,
        unsupported_reason=unsupported_reason,
    )


def _json_playlists(payload: Any, filename: str) -> list[tuple[str, list[Any]]]:
    default_name = _default_playlist_name(filename)
    if isinstance(payload, Mapping):
        raw_playlists = payload.get("playlists")
        if isinstance(raw_playlists, list):
            return [
                _json_playlist(item, default_name, index)
                for index, item in enumerate(raw_playlists)
            ]
        if any(key in payload for key in ("tracks", "items")):
            return [_json_playlist(payload, default_name, 0)]
        if isinstance(payload.get("playlist"), Mapping):
            return [_json_playlist(payload["playlist"], default_name, 0)]
    if isinstance(payload, list):
        if payload and any(
            isinstance(item, Mapping) and any(key in item for key in ("tracks", "items"))
            for item in payload
        ):
            return [_json_playlist(item, default_name, index) for index, item in enumerate(payload)]
        return [(default_name, payload)]
    raise PlaylistImportError(
        "JSON must contain a playlist object, a playlists array, or a track array.",
        code="invalid_document",
        format=ImportFormat.JSON,
    )


def _json_playlist(raw: Any, default_name: str, index: int) -> tuple[str, list[Any]]:
    if not isinstance(raw, Mapping):
        return (f"{default_name} {index + 1}", [raw])
    normalized = _normalize_mapping(raw)
    name = _clean(_first(normalized, _PLAYLIST_ALIASES + ("name", "title"))) or (
        default_name if index == 0 else f"{default_name} {index + 1}"
    )
    tracks = raw.get("tracks", raw.get("items", []))
    if isinstance(tracks, Mapping):
        tracks = tracks.get("items", tracks.get("tracks", []))
    return name, list(tracks) if isinstance(tracks, list) else [tracks]


def _parse_xml_root(source: BinaryIO, context: ParseContext):
    _reject_unsafe_xml(source, context.format)
    _validate_xml_limits(source, context)
    source.seek(0)
    head = source.read(256)
    source.seek(0)
    context.encoding = _xml_encoding(head)
    try:
        root = ElementTree.parse(source).getroot()
    except (DefusedXmlException, ParseError, ValueError) as exc:
        raise PlaylistImportError(
            "XML could not be parsed. Check that tags are balanced and the document is valid.",
            code="invalid_document",
            format=context.format,
        ) from exc
    return root


def _validate_xml_limits(source: BinaryIO, context: ParseContext) -> None:
    source.seek(0)
    element_limit = max(1_000, context.limits.max_tracks * 20)
    element_count = 0
    playlist_count = 0
    track_count = 0
    try:
        for event, element in ElementTree.iterparse(source, events=("start", "end")):
            if event == "start":
                element_count += 1
                local_name = _local_name(element.tag)
                if local_name == "playlist":
                    playlist_count += 1
                    if playlist_count > context.limits.max_playlists:
                        raise ImportLimitExceeded(
                            f"Playlist limit exceeded; this import allows at most "
                            f"{context.limits.max_playlists} playlists.",
                            code="playlist_limit",
                            format=context.format,
                        )
                if local_name in {"track", "media"}:
                    track_count += 1
                    if track_count > context.limits.max_tracks:
                        raise ImportLimitExceeded(
                            f"Track limit exceeded; this import allows at most "
                            f"{context.limits.max_tracks} tracks.",
                            code="track_limit",
                            format=context.format,
                        )
                if element_count > element_limit:
                    raise ImportLimitExceeded(
                        f"XML element limit exceeded; at most "
                        f"{element_limit} elements are allowed.",
                        code="xml_element_limit",
                        format=context.format,
                    )
            else:
                element.clear()
    except ImportLimitExceeded:
        raise
    except (DefusedXmlException, ParseError, ValueError) as exc:
        raise PlaylistImportError(
            "XML could not be parsed. Check that tags are balanced and the document is valid.",
            code="invalid_document",
            format=context.format,
        ) from exc
    finally:
        source.seek(0)


def _validate_json_limits(source: BinaryIO, context: ParseContext) -> None:
    playlist_count = 0
    track_count = 0
    top_item_count = 0
    top_array_playlist_mode = False

    def add_playlist() -> None:
        nonlocal playlist_count
        playlist_count += 1
        if playlist_count > context.limits.max_playlists:
            raise ImportLimitExceeded(
                f"Playlist limit exceeded; this import allows at most "
                f"{context.limits.max_playlists} playlists.",
                code="playlist_limit",
                format=context.format,
            )

    def add_track() -> None:
        nonlocal track_count
        track_count += 1
        if track_count > context.limits.max_tracks:
            raise ImportLimitExceeded(
                f"Track limit exceeded; this import allows at most "
                f"{context.limits.max_tracks} tracks.",
                code="track_limit",
                format=context.format,
            )

    try:
        with _json_binary_reader(source, context) as binary:
            for prefix, event, value in ijson.parse(binary):
                is_track_item = prefix in {"tracks.item", "items.item"} or prefix.endswith(
                    (".tracks.item", ".items.item")
                )
                if is_track_item and event in {
                    "start_map",
                    "start_array",
                    "string",
                    "number",
                    "boolean",
                    "null",
                }:
                    add_track()
                    continue
                if prefix == "playlists.item" and event in {
                    "start_map",
                    "start_array",
                    "string",
                    "number",
                    "boolean",
                    "null",
                }:
                    add_playlist()
                    continue
                if prefix == "playlist" and event == "start_map":
                    add_playlist()
                    continue
                if prefix == "" and event == "map_key" and value in {"tracks", "items"}:
                    if playlist_count == 0:
                        add_playlist()
                    continue
                if prefix == "item" and event == "start_map":
                    top_item_count += 1
                    if top_array_playlist_mode:
                        add_playlist()
                    else:
                        add_track()
                    continue
                if (
                    prefix == "item"
                    and event == "map_key"
                    and value in {"tracks", "items"}
                    and not top_array_playlist_mode
                ):
                    top_array_playlist_mode = True
                    track_count -= top_item_count
                    for _ in range(top_item_count):
                        add_playlist()
                    continue
                if prefix == "item" and event in {"string", "number", "boolean", "null"}:
                    add_track()
    except ImportLimitExceeded:
        raise
    except (ijson.JSONError, ValueError) as exc:
        raise PlaylistImportError(
            "JSON could not be parsed. Check the document syntax and encoding.",
            code="invalid_document",
            format=context.format,
        ) from exc
    finally:
        source.seek(0)


@contextmanager
def _json_binary_reader(
    source: BinaryIO,
    context: ParseContext,
) -> Iterator[BinaryIO]:
    encoding = _detect_text_encoding(source, context.format)
    context.encoding = encoding
    source.seek(0)
    if encoding == "utf-8":
        try:
            yield source
        finally:
            source.seek(0)
        return
    if encoding == "utf-8-sig":
        source.seek(len(codecs.BOM_UTF8))
        try:
            yield source
        finally:
            source.seek(0)
        return

    transcoded = tempfile.SpooledTemporaryFile(
        max_size=context.limits.spool_memory_bytes,
        mode="w+b",
    )
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    try:
        while chunk := source.read(64 * 1024):
            transcoded.write(decoder.decode(chunk).encode("utf-8"))
        transcoded.write(decoder.decode(b"", final=True).encode("utf-8"))
        transcoded.seek(0)
        yield transcoded
    finally:
        transcoded.close()
        source.seek(0)


def _reject_unsafe_xml(source: BinaryIO, format: ImportFormat) -> None:
    source.seek(0)
    overlap = b""
    while chunk := source.read(64 * 1024):
        probe = (overlap + chunk).upper()
        if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
            source.seek(0)
            raise PlaylistImportError(
                "XML document type and entity declarations are not allowed.",
                code="unsafe_xml",
                format=format,
            )
        overlap = probe[-16:]
    source.seek(0)


@contextmanager
def _text_reader(
    source: BinaryIO,
    context: ParseContext,
    *,
    newline: str | None = None,
) -> Iterator[TextIOWrapper]:
    encoding = _detect_text_encoding(source, context.format)
    context.encoding = encoding
    source.seek(0)
    wrapper = TextIOWrapper(source, encoding=encoding, errors="strict", newline=newline)
    try:
        yield wrapper
    finally:
        wrapper.detach()


def _detect_text_encoding(source: BinaryIO, format: ImportFormat) -> str:
    source.seek(0)
    head = source.read(4)
    source.seek(0)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        while chunk := source.read(64 * 1024):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        source.seek(0)
        if format is ImportFormat.M3U8:
            raise PlaylistImportError(
                "M3U8 files must use UTF-8 encoding.",
                code="invalid_encoding",
                format=format,
            ) from exc
        return "cp1252"
    finally:
        source.seek(0)
    return "utf-8"


def _normalize_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {_normalize_key(str(key)): value for key, value in mapping.items() if key is not None}


def _normalize_key(value: str) -> str:
    return _KEY_NOISE.sub("_", value.strip().lower()).strip("_")


def _first(mapping: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    return _first_with_key(mapping, aliases)[1]


def _first_with_key(mapping: Mapping[str, Any], aliases: tuple[str, ...]) -> tuple[str, Any]:
    for alias in aliases:
        normalized_alias = _normalize_key(alias)
        value = mapping.get(normalized_alias)
        if value not in (None, ""):
            return normalized_alias, value
    return "", None


def _parse_duration(value: Any, *, milliseconds: bool = False) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = _clean(value)
        if not text:
            return None
        if ":" in text:
            parts = text.split(":")
            try:
                values = [int(part) for part in parts]
            except ValueError:
                return None
            if len(values) == 2:
                return values[0] * 60 + values[1]
            if len(values) == 3:
                return values[0] * 3600 + values[1] * 60 + values[2]
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if number < 0:
        return None
    if milliseconds:
        number /= 1000
    if not math.isfinite(number) or number > 2_147_483_647:
        return None
    return round(number)


def _split_artist_title(value: str) -> tuple[str | None, str | None]:
    cleaned = _clean(value)
    if not cleaned:
        return None, None
    for separator in (" - ", " – ", " — "):
        artist, found, title = cleaned.partition(separator)
        if found and artist.strip() and title.strip():
            return artist.strip(), title.strip()
    return None, cleaned


def _provider_for_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    lower = uri.lower()
    if lower.startswith("spotify:") or "open.spotify.com/" in lower:
        return "spotify"
    if "music.youtube.com/" in lower or "youtube.com/watch" in lower or lower.startswith(
        "ytmusic:"
    ):
        return "ytmusic"
    if "music.apple.com/" in lower or lower.startswith("applemusic:"):
        return "applemusic"
    if "tidal.com/" in lower or lower.startswith("tidal:"):
        return "tidal"
    return None


def _looks_like_location(value: str) -> bool:
    lower = value.lower()
    return (
        "://" in value
        or ":" in value
        or value.startswith(("/", "\\", "./", "../", "~/"))
        or _DRIVE_PATH.match(value) is not None
        or lower.endswith((".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac"))
    )


def _looks_like_local_location(value: str | None) -> bool:
    if not value:
        return False
    if _provider_for_uri(value):
        return False
    lower = value.lower()
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https"}:
        return False
    return (
        lower.startswith("file:")
        or value.startswith(("/", "\\", "./", "../", "~/"))
        or _DRIVE_PATH.match(value) is not None
        or "/" in value
        or "\\" in value
        or lower.split("?", 1)[0].endswith(
            (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".wma")
        )
    )


def _location_title(uri: str | None) -> str | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    path = unquote(parsed.path or uri).replace("\\", "/")
    name = PurePath(path).name
    if not name:
        return None
    stem = name.rsplit(".", 1)[0]
    return stem.replace("_", " ").strip() or None


def _duplicate_signature(track: Track) -> str | None:
    if track.isrc:
        return f"isrc:{track.isrc.upper()}"
    if track.provider_uris:
        return "uri:" + "|".join(
            f"{key}:{value.lower()}" for key, value in sorted(track.provider_uris.items())
        )
    title = _clean(track.title).lower()
    artist = _clean(track.artist).lower()
    if title and artist:
        return f"song:{title}|{artist}|{_clean(track.album).lower()}|{track.duration_s or ''}"
    source_uri = track.metadata.get("source_uri")
    return f"source:{str(source_uri).lower()}" if source_uri else None


def _elements_named(root, name: str):
    return (element for element in root.iter() if _local_name(element.tag) == name)


def _first_element_text(root, name: str) -> str | None:
    return next(
        (
            _clean(element.text)
            for element in _elements_named(root, name)
            if _clean(element.text)
        ),
        None,
    )


def _direct_child_text(root, name: str) -> str | None:
    return next(
        (
            _clean(child.text)
            for child in list(root)
            if _local_name(child.tag) == name and _clean(child.text)
        ),
        None,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_encoding(head: bytes) -> str:
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    match = _XML_ENCODING.search(head)
    return match.group(1).decode("ascii", errors="replace").lower() if match else "utf-8"


def _isrc(value: str) -> bool:
    return _ISRC.match(re.sub(r"[^A-Za-z0-9]", "", value).upper()) is not None


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _default_playlist_name(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0].strip()
    return stem or "Imported playlist"


def _add_groups(context: ParseContext, groups: OrderedDict[str, list[Track]]) -> None:
    for name, tracks in groups.items():
        context.add_playlist(name, tracks)


def _excerpt(value: str | None) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:160]
