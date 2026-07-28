# Backend — Open Playlist Engine

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · arq · Postgres · Valkey.

## Layout
- `app/core/` — provider-agnostic hub: Open Playlist models, capabilities, plugin
  contract (`adapter.py`), registry, matching, generator model adapters, preflight,
  rate limiting, and security.
- `app/providers/<name>/` — provider adapters (applemusic, spotify, tidal, ytmusic).
  Self-register.
- `app/imports/` — bounded local-file streaming, format registry, normalized
  previews, source loading, and retention cleanup; the same `LocalPlaylistImport`
  lease lifecycle also backs public-URL resolution (SSRF-safe fetching, share-link
  resolvers) and pasted-text parsing.
- `app/db/` — SQLAlchemy models (private data + the evidence graph).
- `app/jobs/` — arq worker, import→match→review→write pipeline, persisted sync
  scheduler, local-import cleanup, durable playlist-organizer jobs, and streamed
  snapshot jobs. Confirmed generator drafts enter the same migration ledger.
- `app/snapshots/` — versioned bundle format, safe filesystem boundary, verification,
  diffing, retention, and storage reconciliation.
- `app/exports/` — versioned portable schemas, serializers, history reconstruction,
  and temporary-file-backed archive generation.
- `app/api/` — FastAPI routers (`/providers`, `/auth`, `/playlists`, `/imports`,
  `/migrations`, `/snapshots`, `/syncs`, `/library`, `/organizer`, `/exports`,
  `/generator`, owner `/shares`, and isolated `/public/shares`).

## Develop
```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --no-access-log # http://localhost:8000
arq app.jobs.worker.WorkerSettings # background worker
pytest
ruff check .
```

## Database
```bash
alembic revision --autogenerate -m "init"   # generate from app/db/models.py
alembic upgrade head
```

## Adding a provider
Implement `ProviderAdapter` (see `app/core/adapter.py`) in `app/providers/<name>/adapter.py`,
declare a `CapabilityDescriptor`, call `register(...)`, and pass the conformance
suite in `tests/conformance/`. Adapters never touch the match graph — they only
read/search/write; `MatchService` owns matching.

Album/artist support is independently structural: implement only the advertised
`SavedAlbumReader`/`SavedAlbumWriter` and
`FollowedArtistReader`/`FollowedArtistWriter` contracts.

## Provider status
| Provider | Read / Search | Migration write | Organizer | Test seam |
|---|---|---|---|---|
| Spotify | ✅ playlists, liked tracks, saved albums, followed artists | ✅ native playlist/library writes | ✅ safe unfollow + exact song removal; no destructive delete | recorded JSON fixtures via injected `httpx.MockTransport` |
| Tidal | ✅ playlists, liked tracks, saved albums, favorite artists | ✅ native playlist/collection writes | ✅ owned-playlist delete; song removal gated off | recorded JSON:API fixtures via injected `httpx.MockTransport` |
| YouTube Music | ✅ device-code/header auth + playlist/Liked Songs read/search | ✅ playlist writes + native likes (`ytmusicapi`) | ✅ owned-playlist delete + `setVideoId` song removal; no verified safe unfollow | injected in-memory client (`client_factory`) |
| Apple Music | ✅ MusicKit user auth + library read and ISRC/text catalog search | ✅ library playlist create/add | read-only; MusicKit lacks delete/remove operations | recorded JSON fixtures via injected `httpx.MockTransport` |

Public URL reads use adapter hooks for unauthenticated YouTube Music playlists and
Apple Music catalog playlists. Spotify and TIDAL URL imports deliberately require
the minimum matching source connection rather than scraping or bypassing provider
access controls.

The unofficial YouTube Music API can't be recorded as stable HTTP, so its seam is
an injected client object instead of a transport. Real singletons use the network;
the conformance suite instantiates the adapter classes directly with a seam, so CI
never makes live calls. See [ADR 0002](../docs/adr/0002-adapter-fixture-testing.md).

## Implemented MVP directions

The implemented self-host paths are capability-driven across Spotify, Tidal,
YouTube Music and Apple Music. Normal playlists migrate according to advertised
capabilities, while Spotify Liked Songs, Tidal My Collection, and YouTube Music
Liked Songs map to each provider's native liked/saved library. Docker Compose
applies Alembic migrations before starting the backend and worker. For local
development, run `alembic upgrade head` before `uvicorn` and `arq`. Playlist
detail and migration item review endpoints support track-level selection,
partial-migration labels, duplicate skips, batch review actions, and low-confidence
match correction in the UI. Migration creation supports explicit album/artist job
items, conservative matching, native contains checks, review, and entity-specific
statistics. It performs a preflight that warns
before exceeding the conservative defaults: 1 playlist/job, 50 tracks/job, 250
tracks/day, and 120 seconds between jobs.

The generator keeps raw prompts out of the database. `app/core/generator.py` validates
bounded structured output from a local OpenAI-compatible endpoint or optional GitHub
Copilot SDK session, then resolves every suggestion through the target adapter and
`MatchService`. `app/api/generator.py` owns private preference summaries and editable
drafts. Confirmation snapshots approved URIs into `MigrationJob`/`JobItem` rows with
`source_kind="generated"`; the worker skips source credentials and never rematches a
reviewed URI. See
[`docs/PLAYLIST_GENERATOR.md`](../docs/PLAYLIST_GENERATOR.md).

Local-file imports accept raw request bodies at `/api/imports/preview`, persist
only normalized Open Playlist JSON with an expiry, and enter the same migration
worker without a provider credential. Supported formats, CSV aliases, limits,
retention, and API examples are in
[`docs/LOCAL_FILE_IMPORTS.md`](../docs/LOCAL_FILE_IMPORTS.md).

Public-URL and pasted-text imports reuse the identical `LocalPlaylistImport`
table and lease lifecycle: `POST /api/imports/url-preview` and
`POST /api/imports/text-preview` resolve the source (via the strict SSRF-safe
fetcher, a provider's public-read adapter hook, or an owner's connected source
credential as a fallback) and persist one normalized preview snapshot, keyed by
`source_kind` (`local_file`/`public_url`/`pasted_text`) and an optional
`source_provider`/`source_label`/`source_locator`/`source_fingerprint`. Migration
jobs reference the import record's own ID as `source_account_id`, so queueing,
claiming, failure handling, and cleanup are identical across all three import
kinds. When a URL points at this deployment's own `/share/{token}` links, the
public share snapshot is fetched and normalized directly instead of being
treated as an arbitrary external page — no other scraping is supported.

The worker also runs the playlist sync scheduler at startup and every minute.
`sync_rule`, `sync_run`, and `sync_checkpoint` persist schedules, active-run leases,
source/target snapshots, target mappings, results and errors. Sync-created migration
jobs are hidden from normal migration history/statistics but remain available through
their progress/review endpoints. See
[`docs/SYNCHRONIZATION.md`](../docs/SYNCHRONIZATION.md).
Playlist Organizer uses separate `organizer_job` and `organizer_item` tables. The
worker persists per-playlist results, skips successful items on retry, rate-limits
provider/account writes, and invalidates playlist caches after successful work.
Provider behavior and recovery limits are documented in
[`docs/PLAYLIST_ORGANIZER.md`](../docs/PLAYLIST_ORGANIZER.md).

Portable exports read one playlist at a time and stream temporary CSV, TXT, M3U8,
XSPF, JSON, or ZIP64 artifacts. Live selections use `POST /api/exports`; terminal
migration history uses `POST /api/exports/migrations/{job_id}` while item details
remain within retention. The default limit is 100 playlists per request
(`OPE_EXPORT_MAX_PLAYLISTS`) with no track cap. See
[`docs/EXPORTING_PLAYLISTS.md`](../docs/EXPORTING_PLAYLISTS.md).

The existing migration stats API also exposes complete history details. Track,
album, and artist rows support owner-scoped filters and optional paging, while
`GET /api/migrations/{job_id}/report` streams versioned CSV or JSON exports without
materializing the full result. Item detail defaults to 90-day retention; the ARQ
worker snapshots per-entity summaries and removes expired job/operation rows in
bounded hourly batches. Accepted mixed-entity review decisions remain available for
future matching. See
[`docs/MIGRATION_HISTORY.md`](../docs/MIGRATION_HISTORY.md).

Provider setup steps are documented in
[`docs/CONNECTING_PROVIDERS.md`](../docs/CONNECTING_PROVIDERS.md).

## Local snapshots

`POST /api/snapshots/profiles/{id}/snapshots` queues a streamed snapshot job.
Profiles can include collections from multiple connected accounts. The worker reads
each collection with `iter_playlist_items`, writes canonical JSONL directly into a
ZIP64 Open Playlist bundle, records partial provider failures, verifies checksums,
and applies deterministic count/age retention. The API owns profile/history CRUD,
storage usage, verification, diff, download, portable import, deletion, and cleanup.

Snapshot restore uses `source_snapshot_id` on the existing migration endpoints.
Only source reading changes; target preflight, matching, review, chunked writes,
duplicate detection, operation ledger, SSE progress, and statistics remain the same.
Snapshot lineage is isolated from deleted/reconnected live-account history.

Set `OPE_SNAPSHOT_DIR` to a durable directory writable by both API and worker.
Docker Compose mounts the shared `snapshots` volume at `/data/snapshots`. Full
format and operations documentation is in [`docs/SNAPSHOTS.md`](../docs/SNAPSHOTS.md).

Public snapshot models, hashed/encrypted share tokens, owner sessions, recipient
account isolation, portable downloads, and share-backed migration jobs are
documented in [`docs/PLAYLIST_SHARING.md`](../docs/PLAYLIST_SHARING.md).
