# Exporting playlists

Open Playlist Engine can download live source selections and the source snapshot from
completed or failed migration history. Export generation stays on the local instance;
it does not use email, cloud storage, or a hosted account.

## UI usage

1. Choose and connect a source provider. A target provider is optional.
2. Select one or more playlists. Load a playlist to export only selected tracks.
3. Choose a format under **Local file**, then select **Download export**.
4. To export migration history, open **Stats**, choose a completed or failed
   migration, then use **Download history**.

Warnings appear after the download and are represented in the exported fields,
format comments/extensions, JSON bundle, or ZIP manifest.

## Formats

| Format | Extension | Content type | Encoding | Representation |
|---|---|---|---|---|
| CSV v1 | `.csv` | `text/csv` | UTF-8 with BOM | One ordered track row; an empty playlist has one metadata-only row. |
| TXT v1 | `.txt` | `text/plain` | UTF-8 | Tab-delimited v1 columns after two header comments. |
| M3U v1 | `.m3u8` | `application/vnd.apple.mpegurl` | UTF-8 | Extended M3U with ordered `#EXTINF` entries and `#OPE-*` metadata. |
| XSPF v1 | `.xspf` | `application/xspf+xml` | UTF-8 XML 1.0 | Standard XSPF elements plus the Open Playlist v1 extension namespace. |
| Open Playlist JSON v1 | `.json` | `application/vnd.open-playlist+json` | UTF-8 | Lossless bundle of universal `Playlist` and ordered `Track` models. |

M3U and M3U8 are the same v1 serializer; `.m3u8` makes the required UTF-8 encoding
explicit. Known `spotify:track:*` and `tidal:track:*` URIs become usable web URLs in
M3U8/XSPF locations. The original source URI remains in Open Playlist metadata.

CSV and TXT columns are stable for schema version 1:

```text
schema_version, playlist_id, playlist_name, playlist_description, playlist_kind,
playlist_owner_id, playlist_artwork_uri, playlist_created_at, playlist_updated_at,
order, source_position, track_id, source_item_id, title, artist, album,
duration_seconds, isrc, source_uri, artwork_uri, added_at, media_type,
unsupported_reason
```

Cells whose first meaningful character could start a spreadsheet formula are prefixed
with an apostrophe in CSV/TXT. JSON is the lossless format and does not alter values.

## Open Playlist JSON bundle v1

Bundle documents use:

```json
{
  "$schema": "https://openplaylistengine.dev/schemas/export/open-playlist-bundle-v1.json",
  "schema_version": 1,
  "source_provider": "spotify",
  "playlists": [],
  "warnings": []
}
```

`playlists` contains the universal `Playlist` model. Track list order is authoritative,
and represented metadata includes IDs, title, artist, album, duration, release data,
credits, label, ISRC, artwork, provider URIs, provider metadata, source position,
media type, local-file state, source item ID, added date, and unsupported reason.
Readers must reject unsupported `schema_version` values rather than guessing.

The backend parser validates the bundle back into the same Pydantic models. Serializer
round-trip tests cover multiple playlists, rich track metadata, and empty playlists.

## Multi-playlist ZIP and manifest v1

Every multi-playlist download is a ZIP64 archive:

- CSV, TXT, M3U8, and XSPF contain one file per playlist plus `manifest.json`.
- JSON contains `open-playlist-bundle.json` plus `manifest.json`.

The manifest schema URI is
`https://openplaylistengine.dev/schemas/export/open-playlist-export-manifest-v1.json`.
It records schema version, requested format, source provider, playlist count, ordered
entries, per-entry status/track count/warning codes, and full warnings.

Playlist names are normalized to ASCII-safe basenames, path separators and traversal
segments are removed, Windows reserved names are prefixed, and trailing unsafe
characters are stripped. Empty names fall back to the playlist ID. Collisions are
resolved case-insensitively with deterministic `-2`, `-3`, and later suffixes.

## Warnings and errors

Valid output can include:

| Warning code | Meaning |
|---|---|
| `partial_selection` | Only selected tracks were exported. |
| `empty_playlist` | The playlist or selected subset has zero tracks. |
| `unsupported_items` | Episodes, videos, local files, or unknown media are present. |
| `missing_source_uri` | One or more tracks have no provider URI for M3U8/XSPF playback. |
| `playlist_read_failed` | One playlist in a multi-playlist request was unavailable or denied. |
| `historical_playlist_metadata_partial` | An older/invalid history snapshot lacks playlist-level fields. |
| `historical_track_metadata_partial` | Invalid historical track JSON was rebuilt from migration columns. |

A single unreadable playlist returns an explicit HTTP error. A multi-playlist export
continues for per-playlist access/not-found failures and records placeholders in the
manifest; if every playlist fails, no archive is returned. Authentication expiry and
rate limiting always abort immediately.

## Limits and resource handling

- `OPE_EXPORT_MAX_PLAYLISTS` defaults to `100`.
- Track count is not capped.
- Provider reads and serializers process one playlist at a time.
- Output is written to a temporary file; ZIP entries are streamed with ZIP64 enabled.
- Responses stream in 64 KiB chunks.
- Temporary artifacts are deleted after completion, client cancellation, or any
  generation error.
- The instance temporary filesystem must have enough free space for the completed
  file or archive.

## API

Live selection:

```http
POST /api/exports
Content-Type: application/json

{
  "source_provider": "spotify",
  "source_account_id": "account-id",
  "format": "json",
  "selection": {
    "playlist_ids": ["playlist-id"],
    "tracks": {"playlist-id": ["source-track-id"]}
  }
}
```

History:

```http
POST /api/exports/migrations/{job_id}
Content-Type: application/json

{"format": "xspf"}
```

The response includes `Content-Disposition`, `Content-Length`,
`X-Open-Playlist-Schema-Version: 1`, and
`X-Open-Playlist-Warning-Count`. Runtime request/response schemas are published by
FastAPI at `/openapi.json`.
