# Configuration

Open Playlist Engine reads backend settings from environment variables with the
`OPE_` prefix. Copy the root example before starting:

```bash
cp .env.example .env
```

Keep `.env` local. It can contain provider secrets, private keys, model credentials,
and tokens. [`.env.example`](../.env.example) is the canonical list of settings and
defaults.

## Base settings

| Setting | Default | Purpose |
|---|---|---|
| `OPE_DEPLOYMENT_MODE` | `self_host` | Selects `self_host` or `hosted` behavior |
| `OPE_FRONTEND_URL` | `http://localhost:8080` | Browser origin used for redirects and CORS |
| `OPE_SECRET_KEY` | Placeholder in `.env.example` | Encrypts persisted provider credentials and signs sessions |
| `OPE_DATABASE_URL` | Local development default | Postgres connection; Docker Compose supplies its own value |
| `OPE_VALKEY_URL` | Local development default | Valkey connection; Docker Compose supplies its own value |

Replace `OPE_SECRET_KEY` before connecting accounts. Use a high-entropy value of at
least 32 characters; public sharing will remain disabled when the placeholder is
used.

`self_host` resolves the migration owner server-side and permits the local YouTube
Music header-paste fallback. `hosted` disables that fallback and fails closed until
a real user-authentication dependency is configured.

## Provider credentials

Only configure providers you intend to use.

| Provider | Main settings |
|---|---|
| Spotify | `OPE_SPOTIFY_CLIENT_ID`, optional `OPE_SPOTIFY_CLIENT_SECRET`, `OPE_SPOTIFY_REDIRECT_URI` |
| Tidal | `OPE_TIDAL_CLIENT_ID`, optional `OPE_TIDAL_CLIENT_SECRET`, `OPE_TIDAL_REDIRECT_URI` |
| YouTube Music | `OPE_YTMUSIC_ENABLED`, `OPE_YTMUSIC_CLIENT_ID`, `OPE_YTMUSIC_CLIENT_SECRET`, `OPE_YOUTUBE_OFFICIAL_ENABLED` |
| Apple Music | `OPE_APPLE_MUSIC_TEAM_ID`, `OPE_APPLE_MUSIC_KEY_ID`, and `OPE_APPLE_MUSIC_PRIVATE_KEY_PATH` or `OPE_APPLE_MUSIC_PRIVATE_KEY` |

App registration, required scopes, callback URLs, and authentication fallbacks are
documented in [Connecting providers](CONNECTING_PROVIDERS.md).

## Feature settings

Settings are grouped by a common prefix wherever possible:

| Area | Settings | Details |
|---|---|---|
| Matching and migration | `OPE_REVIEW_CONFIDENCE_THRESHOLD`, `OPE_MIGRATION_SAFE_*`, `OPE_MIGRATION_WORKER_JOB_TIMEOUT_S` | [Design](DESIGN.md#safe-migration-defaults) |
| Migration history | `OPE_MIGRATION_HISTORY_*`, `OPE_MIGRATION_REPORT_BATCH_SIZE` | [History and reports](MIGRATION_HISTORY.md) |
| Local file imports | `OPE_LOCAL_IMPORT_*` | [Local imports](LOCAL_FILE_IMPORTS.md#limits-and-configuration) |
| URL and text imports | `OPE_IMPORT_*` | [Import sources](IMPORT_SOURCES.md#limits) |
| Exports | `OPE_EXPORT_MAX_PLAYLISTS` | [Portable exports](EXPORTING_PLAYLISTS.md#limits-and-resource-handling) |
| Generator | `OPE_GENERATOR_*` | [Playlist generator](PLAYLIST_GENERATOR.md#local-openai-compatible-setup) |
| Synchronization | `OPE_SYNC_*` | [Scheduled sync](SYNCHRONIZATION.md#configuration) |
| Organizer | `OPE_ORGANIZER_*` | [Playlist Organizer](PLAYLIST_ORGANIZER.md#durable-jobs-and-retries) |
| Snapshots | `OPE_SNAPSHOT_*` | [Local snapshots](SNAPSHOTS.md#storage-and-persistence) |
| Public sharing | `OPE_PUBLIC_BASE_URL`, `OPE_OWNER_*`, `OPE_SHARE_*` | [Playlist sharing](PLAYLIST_SHARING.md#configure-the-instance) |

## Migration guardrails

The default migration limits are intentionally conservative:

- 1 playlist per job;
- 50 tracks per job;
- 250 tracks per day;
- 120 seconds between jobs.

The UI can override these values only after showing a warning. Worker jobs can run
for up to 3,600 seconds by default so large playlists do not inherit ARQ's shorter
default timeout.

## Operational relationships

- `OPE_LOCAL_IMPORT_QUEUED_RETENTION_S` must exceed
  `OPE_MIGRATION_WORKER_JOB_TIMEOUT_S`.
- `OPE_SYNC_STALE_RUN_AFTER_S` should remain longer than the worker timeout so a live
  sync is not recovered as stale.
- The API and worker must use the same `OPE_SNAPSHOT_DIR`. Docker Compose mounts the
  shared `snapshots` volume at `/data/snapshots`.
- Public sharing stays disabled while `OPE_PUBLIC_BASE_URL` is empty. Enabling it
  also requires a separate strong `OPE_OWNER_ACCESS_TOKEN`.
- Portable exports default to 100 playlists per download and do not impose a track
  limit.

Review [`.env.example`](../.env.example) before changing limits; it records every
available variable and the expected units.
