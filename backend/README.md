# Backend — Open Playlist Engine

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · arq · Postgres · Valkey.

## Layout
- `app/core/` — provider-agnostic hub: Open Playlist models, capabilities, plugin
  contract (`adapter.py`), registry, `match_service.py`, rate limiting, security.
- `app/providers/<name>/` — provider adapters (applemusic, spotify, tidal, ytmusic).
  Self-register.
- `app/db/` — SQLAlchemy models (private data + the evidence graph).
- `app/jobs/` — arq worker + the import→match→review→write pipeline.
- `app/exports/` — versioned portable schemas, serializers, history reconstruction,
  and temporary-file-backed archive generation.
- `app/api/` — FastAPI routers (`/providers`, `/auth`, `/playlists`, `/library`,
  `/migrations`, `/exports`, owner `/shares`, and isolated `/public/shares`).

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
| Provider | Read / Search | Write | Test seam |
|---|---|---|---|
| Spotify | ✅ playlists, liked tracks, saved albums, followed artists | ✅ native playlist/library writes | recorded JSON fixtures via injected `httpx.MockTransport` |
| Tidal | ✅ playlists, liked tracks, saved albums, favorite artists | ✅ native playlist/collection writes | recorded JSON:API fixtures via injected `httpx.MockTransport` |
| YouTube Music | ✅ device-code/header auth + playlist/Liked Songs read/search | ✅ playlist writes + native likes (`ytmusicapi`) | injected in-memory client (`client_factory`) |
| Apple Music | ✅ MusicKit user auth + library read and ISRC/text catalog search | ✅ library playlist create/add | recorded JSON fixtures via injected `httpx.MockTransport` |

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
Public snapshot models, hashed/encrypted share tokens, owner sessions, recipient
account isolation, portable downloads, and share-backed migration jobs are
documented in [`docs/PLAYLIST_SHARING.md`](../docs/PLAYLIST_SHARING.md).
