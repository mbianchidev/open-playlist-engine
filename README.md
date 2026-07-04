# Open Playlist Engine

Any-to-any music **playlist migration** — move playlists between Spotify, YouTube
Music, Tidal, Deezer, Apple Music and more, in any direction, through a sleek UI.

This is the first reference implementation of the
[`open-playlist`](https://github.com/mbianchidev/open-playlist) universal
`Playlist`/`Track` spec. Architecture is **hub-and-spoke**: every provider is a
plugin spoke, the universal format is the hub, so adding a provider is O(1) and it
instantly works with all the others — both as source and target.

> Status: **early MVP**. The self-hosted Spotify → YouTube Music path is wired:
> Spotify OAuth/read/search, YouTube Music header auth/read/search/write,
> persisted credentials, playlist/track selection, partial-migration detection,
> migration jobs, review actions and SSE progress. Other provider directions
> remain gated until their adapters advertise implemented capabilities. See
> [`docs/DESIGN.md`](docs/DESIGN.md).

## How it works

```
source provider ─ read ─▶ [ Open Playlist hub ] ─ write ─▶ target provider
                              (identity graph)
```

Pipeline: **import → match → review → write**, with durable, replayable progress.
Matching is ISRC-first with a self-enriching evidence graph and a human review step
for low-confidence matches.

## Layout

| Path | What |
|---|---|
| `backend/` | FastAPI app, provider adapters, matching, jobs, DB. See [`backend/README.md`](backend/README.md). |
| `frontend/` | Vite + React SPA, consumes the backend OpenAPI. See [`frontend/README.md`](frontend/README.md). |
| `openapi/` | Vendored [`open-playlist`](https://github.com/mbianchidev/open-playlist) spec the universal `Playlist`/`Track` model mirrors. |
| `docs/` | [`DESIGN.md`](docs/DESIGN.md) and [ADRs](docs/adr). |

Frontend and backend are **hard-separated** — no shared code; the FE talks only to
the generated OpenAPI client.

## Quickstart (Docker)

```bash
cp .env.example .env        # then set OPE_SECRET_KEY and Spotify creds
docker compose build --no-cache
docker compose up
```

- Frontend: http://localhost:8080
- Backend API + docs: http://localhost:8000/docs · health: http://localhost:8000/health

## Local development

```bash
# Backend
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload          # :8000
arq app.jobs.worker.WorkerSettings     # background worker
pytest && ruff check .

# Frontend (separate shell)
cd frontend && npm install
npm run dev                            # :5173, proxies /api to :8000
npm run build
```

## Configuration

All backend settings use the `OPE_` env prefix; see [`.env.example`](.env.example).
Key flags: `OPE_DEPLOYMENT_MODE` (`self_host`/`hosted`), `OPE_YTMUSIC_ENABLED`,
`OPE_YTMUSIC_CLIENT_ID`, `OPE_YTMUSIC_CLIENT_SECRET`,
`OPE_YOUTUBE_OFFICIAL_ENABLED`, `OPE_SECRET_KEY`, `OPE_FRONTEND_URL`.
Safe migration defaults are intentionally slow and can be overridden only after a
warning in the UI: 1 playlist/job, 50 tracks/job, 250 tracks/day, and 120 seconds
between jobs (`OPE_MIGRATION_SAFE_*`).

## Spotify → YouTube Music

1. Create a Spotify app at <https://developer.spotify.com/dashboard> and set its
   redirect URI to `http://127.0.0.1:8000/api/auth/spotify/callback`.
2. Put `OPE_SPOTIFY_CLIENT_ID`, optional `OPE_SPOTIFY_CLIENT_SECRET`,
   `OPE_YTMUSIC_CLIENT_ID`, `OPE_YTMUSIC_CLIENT_SECRET`, `OPE_SECRET_KEY`, and
   `OPE_FRONTEND_URL` in `.env`.
3. Start Docker Compose, open `http://localhost:8080`, choose Spotify as source
   and YouTube Music as target.
4. Connect Spotify in the popup.
5. For YouTube Music, open the verification URL shown by the app and enter the
   device code. If Google blocks the unverified OAuth app, or if YouTube Music
   OAuth credentials are not set, use the guided browser-session header fallback
   shown in the connection panel.
6. Pick one playlist, optionally choose individual tracks, and start the migration.
   The UI warns before exceeding the safe defaults or before writing into a target
   playlist that has the same name but different songs.
7. When the job finishes, the progress panel says "Migration succeeded" and links
   to created target playlists when the target provider exposes a web URL.
8. Review low-confidence matches in the progress panel: approve the suggested
   YouTube Music URI, approve all suggested matches, paste a corrected URI/video
   ID, skip one item, or deny all doubtful items.
9. Re-running a playlist reuses an existing migrated target playlist, labels
   partial source playlists/tracks, and can migrate only new source songs for
   already migrated playlists. Duplicate target songs are skipped with an item
   notice instead of being added twice.

Detailed Spotify app and YouTube Music header-copy steps are in
[`docs/CONNECTING_PROVIDERS.md`](docs/CONNECTING_PROVIDERS.md).

## Adding a provider

Implement `ProviderAdapter` in `backend/app/providers/<name>/adapter.py`, declare a
`CapabilityDescriptor`, `register(...)` it, and pass the conformance suite in
`backend/tests/conformance/`. Adapters only read/search/write — the core
`MatchService` owns matching. Details in [`docs/DESIGN.md`](docs/DESIGN.md) §5.

## License

MIT
