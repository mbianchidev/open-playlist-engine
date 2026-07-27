# Backend — Open Playlist Engine

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · arq · Postgres · Valkey.

## Layout
- `app/core/` — provider-agnostic hub: Open Playlist models, capabilities, plugin
  contract (`adapter.py`), registry, `match_service.py`, rate limiting, security.
- `app/providers/<name>/` — provider adapters (applemusic, spotify, tidal, ytmusic).
  Self-register.
- `app/db/` — SQLAlchemy models (private data + the evidence graph).
- `app/jobs/` — arq worker + the import→match→review→write pipeline.
- `app/api/` — FastAPI routers (`/providers`, `/auth`, `/playlists`, `/library`,
  `/migrations`).

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
match correction in the UI. migration creation supports explicit album/artist job items, conservative matching,
native contains checks, review, and entity-specific statistics. It performs a preflight that warns
before exceeding the conservative defaults: 1 playlist/job, 50 tracks/job, 250
tracks/day, and 120 seconds between jobs.

Provider setup steps are documented in
[`docs/CONNECTING_PROVIDERS.md`](../docs/CONNECTING_PROVIDERS.md).
