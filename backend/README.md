# Backend — Open Playlist Engine

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · arq · Postgres · Valkey.

## Layout
- `app/core/` — provider-agnostic hub: Open Playlist models, capabilities, plugin
  contract (`adapter.py`), registry, `match_service.py`, rate limiting, security.
- `app/providers/<name>/` — provider adapters (applemusic, spotify, tidal, ytmusic).
  Self-register.
- `app/db/` — SQLAlchemy models (private data + the evidence graph).
- `app/jobs/` — arq worker + migration and streamed snapshot jobs.
- `app/snapshots/` — versioned bundle format, safe filesystem boundary, verification,
  diffing, retention, and storage reconciliation.
- `app/api/` — FastAPI routers (`/providers`, `/auth`, `/playlists`, `/migrations`,
  `/snapshots`).

## Develop
```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload      # http://localhost:8000  (/docs, /health)
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

## Provider status
| Provider | Read / Search | Write | Test seam |
|---|---|---|---|
| Spotify | ✅ OAuth + playlist/saved-library read/search | ✅ current playlist + saved-library writes | recorded JSON fixtures via injected `httpx.MockTransport` |
| Tidal | ✅ OAuth + playlist/My Collection read/search | ✅ playlist + My Collection writes | recorded JSON:API fixtures via injected `httpx.MockTransport` |
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
match correction in the UI. Migration creation performs a preflight that warns
before exceeding the conservative defaults: 1 playlist/job, 50 tracks/job, 250
tracks/day, and 120 seconds between jobs.

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
