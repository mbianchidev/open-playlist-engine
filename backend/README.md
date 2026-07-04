# Backend — Open Playlist Engine

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · arq · Postgres · Valkey.

## Layout
- `app/core/` — provider-agnostic hub: Open Playlist models, capabilities, plugin
  contract (`adapter.py`), registry, `match_service.py`, rate limiting, security.
- `app/providers/<name>/` — provider adapters (spotify, ytmusic). Self-register.
- `app/db/` — SQLAlchemy models (private data + the evidence graph).
- `app/jobs/` — arq worker + the import→match→review→write pipeline.
- `app/api/` — FastAPI routers (`/providers`, `/auth`, `/playlists`, `/migrations`).

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
| Spotify | ✅ OAuth + live read/search (Web API over `httpx`) | stub | recorded JSON fixtures via injected `httpx.MockTransport` |
| YouTube Music | ✅ device-code/header auth + library read/search (`ytmusicapi`) | ✅ live write (`ytmusicapi`) | injected in-memory client (`client_factory`) |

The unofficial YouTube Music API can't be recorded as stable HTTP, so its seam is
an injected client object instead of a transport. Real singletons use the network;
the conformance suite instantiates the adapter classes directly with a seam, so CI
never makes live calls. See [ADR 0002](../docs/adr/0002-adapter-fixture-testing.md).

## Spotify → YouTube Music MVP

The implemented self-host path uses Spotify as the source and YouTube Music as the
target. Docker Compose applies Alembic migrations before starting the backend and
worker. For local development, run `alembic upgrade head` before `uvicorn` and
`arq`. Playlist detail and migration item review endpoints support track-level
selection, partial-migration labels, delta-only reruns for already migrated
playlists, duplicate skips, batch review actions, and low-confidence match
correction in the UI. Migration creation performs a preflight
that warns before exceeding the conservative defaults: 1 playlist/job, 50
tracks/job, 250 tracks/day, and 120 seconds between jobs.

Provider setup steps are documented in
[`docs/CONNECTING_PROVIDERS.md`](../docs/CONNECTING_PROVIDERS.md).
