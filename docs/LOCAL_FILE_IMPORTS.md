# Local playlist-file imports

Open Playlist Engine can use a local playlist file as the source for the normal
match, review, write, and progress pipeline. The application reads playlist
metadata only. It never opens referenced audio paths, uploads audio, or sends the
playlist file to an external parsing service.

## Supported formats

| Format | Supported input |
|---|---|
| TXT | `Artist - Title` per line, or tab-separated `title`, `artist`, `album`, `duration`, `isrc`, `uri`, `playlist` fields. `#PLAYLIST:Name` starts or names a playlist. |
| CSV | Canonical schema and common header aliases described below. A playlist column can create multiple playlists in one file. |
| M3U / M3U8 | `#EXTM3U`, `#PLAYLIST`, and `#EXTINF` metadata plus ordered locations. M3U8 must be UTF-8. |
| PLS | Indexed `FileN`, `TitleN`, and `LengthN` entries plus `Title` and `NumberOfEntries`. |
| WPL | Windows Media Player SMIL playlists and available media attributes. |
| XSPF | Playlist title, track location, title, creator, album, duration, and identifier metadata. |
| XML | Generic `<playlist>` / `<track>` documents using attributes or child fields. |
| JSON | A single playlist object, a `playlists` array, an array of playlists, or a track array. Nested `{"track": ...}` items are accepted. |

Content detection takes precedence over a misleading extension and reports the
mismatch in the preview.

## Canonical CSV

Use this header for portable exports:

```csv
playlist,title,artist,album,duration_s,isrc,uri
Road Trip,Déjà Vu,Beyoncé,B'Day,239,USSM10603689,spotify:track:abc
```

Accepted aliases are case-insensitive and ignore spaces, punctuation, and
underscores:

| Canonical field | Common aliases |
|---|---|
| `playlist` | `playlist_name`, `playlistname`, `list` |
| `title` | `track`, `track_title`, `tracktitle`, `song`, `song_title`, `name` |
| `artist` | `artists`, `artist_name`, `track_artist`, `creator`, `performer` |
| `album` | `album_name`, `album_title`, `release` |
| `duration_s` | `duration`, `duration_seconds`, `length`, `duration_ms`, `length_ms` |
| `isrc` | `recording_isrc` |
| `uri` | `url`, `location`, `path`, `file`, `provider_uri`, `track_uri`, `link` |

Durations may be seconds, milliseconds when the header says `_ms`, `MM:SS`, or
`HH:MM:SS`.

## Preview and migration

1. Choose **Local playlist file** as the source and a connected streaming
   provider as the target.
2. Upload one supported file. The preview shows the detected format, playlist and
   track counts, duplicates, malformed items, unsupported local paths, and other
   lossy conversions.
3. Select playlists and individual migratable tracks. Duplicates remain in their
   original order. Unsupported entries remain visible but are not selected.
4. Start the migration. The target uses the same match, review, duplicate-write
   protection, progress, and statistics flow as provider-backed sources.

The raw upload is closed immediately after parsing. The normalized preview is
stored in Postgres only until it expires or is consumed by a migration. Successful
jobs delete it atomically. Failed or cancelled jobs retain it for a short retry
grace, then the worker cleanup job removes it. Unused previews are deleted after
the configured retention period.

## Limits and configuration

| Environment variable | Default | Purpose |
|---|---:|---|
| `OPE_LOCAL_IMPORT_MAX_BYTES` | `10485760` | Maximum request body, 10 MiB. |
| `OPE_LOCAL_IMPORT_MAX_PLAYLISTS` | `100` | Maximum playlists in one file. |
| `OPE_LOCAL_IMPORT_MAX_TRACKS` | `5000` | Maximum tracks across the file. |
| `OPE_LOCAL_IMPORT_MAX_ISSUES` | `200` | Detailed preview findings retained before a summary marker. |
| `OPE_LOCAL_IMPORT_SPOOL_MEMORY_BYTES` | `1048576` | Bytes kept in memory before the temporary upload stream spills to disk. |
| `OPE_LOCAL_IMPORT_RETENTION_S` | `3600` | Lifetime of an unused preview. |
| `OPE_LOCAL_IMPORT_QUEUED_RETENTION_S` | `7200` | Lease while a migration waits for or runs in the worker. |
| `OPE_LOCAL_IMPORT_FAILED_RETENTION_S` | `900` | Retry grace after failure or cancellation. |

The packaged nginx proxy disables request buffering for the preview endpoint and
uses the same 10 MiB default. If `OPE_LOCAL_IMPORT_MAX_BYTES` changes, update
`client_max_body_size` in `frontend/nginx.conf` to match.
`OPE_LOCAL_IMPORT_QUEUED_RETENTION_S` must remain greater than
`OPE_MIGRATION_WORKER_JOB_TIMEOUT_S`; an expired lease marks a stalled job failed
and removes its normalized source data.

## API

The preview endpoint accepts the file as the raw request body so FastAPI can
enforce the limit while streaming instead of buffering a multipart upload first:

```bash
curl --data-binary @road-trip.csv \
  "http://localhost:8000/api/imports/preview?filename=road-trip.csv"
```

`GET /api/imports/{id}` retrieves an owner-scoped, unexpired preview.
`DELETE /api/imports/{id}` discards an unused preview. Arbitrary server paths are
never accepted.
