# Open Playlist Engine

Any-to-any music **playlist migration** — move playlists between Spotify, YouTube
Music, Tidal, Deezer, Apple Music and more, in any direction, through a sleek UI.

This is the first reference implementation of the
[`open-playlist`](https://github.com/mbianchidev/open-playlist) universal
`Playlist`/`Track` spec. Architecture is **hub-and-spoke**: every provider is a
plugin spoke, the universal format is the hub, so adding a provider is O(1) and it
instantly works with all the others — both as source and target.

> Status: **early MVP**. Spotify, Tidal, YouTube Music and Apple Music advertise
> implemented directions dynamically: Spotify OAuth/read/search/write, Tidal
> OAuth/read/search/write, YouTube Music device/header auth/read/search/write, and
> official Apple MusicKit library read/search/write. Persisted credentials,
> playlist/track selection, partial-migration detection, migration jobs, review
> actions, SSE progress, migration statistics, and opt-in immutable public
> playlist shares are wired. Other provider
> directions remain gated until
> their adapters advertise implemented capabilities. See
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
cp .env.example .env        # then set OPE_SECRET_KEY and provider OAuth creds
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
uvicorn app.main:app --reload --no-access-log  # :8000
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
`OPE_YOUTUBE_OFFICIAL_ENABLED`, `OPE_SPOTIFY_CLIENT_ID`,
`OPE_SPOTIFY_CLIENT_SECRET`, `OPE_TIDAL_CLIENT_ID`, `OPE_TIDAL_CLIENT_SECRET`,
`OPE_APPLE_MUSIC_TEAM_ID`,
`OPE_APPLE_MUSIC_KEY_ID`, `OPE_APPLE_MUSIC_PRIVATE_KEY_PATH`,
`OPE_SECRET_KEY`, `OPE_FRONTEND_URL`. Public playlist sharing additionally uses
`OPE_PUBLIC_BASE_URL` and `OPE_OWNER_ACCESS_TOKEN`; it remains disabled while the
public URL is empty.
Self-host mode resolves the migration owner server-side as the local user. Hosted
mode fails closed until a real user-authentication dependency is configured; it
does not accept a caller-provided user ID.
Safe migration defaults are intentionally slow and can be overridden only after a
warning in the UI: 1 playlist/job, 50 tracks/job, 250 tracks/day, and 120 seconds
between jobs (`OPE_MIGRATION_SAFE_*`). Worker jobs can run for up to 3600 seconds
by default (`OPE_MIGRATION_WORKER_JOB_TIMEOUT_S`) so large playlists do not hit
ARQ's 5-minute default timeout.

## Self-hosted playlist sharing

The **Sharing** workspace publishes an immutable, metadata-only playlist snapshot
behind a 256-bit revocable token. Recipients can view it, download Open Playlist
JSON, CSV, TXT, M3U8, or XSPF, then connect their own target account and use the
existing match/review/write flow. Public visitors never see or write through the
owner's connected accounts.

Sharing is off by default. To enable it, configure a public HTTPS URL, a separate
strong owner access token, and public Spotify/Tidal callback URLs when those
recipient targets are needed. Public and unlisted links differ in search-engine
indexing; both require the unguessable URL. Snapshots do not follow later source
changes. Owners can change visibility, expire, or revoke a link at any time.

See [`docs/PLAYLIST_SHARING.md`](docs/PLAYLIST_SHARING.md) for setup, reverse
proxy, security, recipient credential retention, and usage details.

## Spotify, Tidal, YouTube Music and Apple Music

1. Create a Spotify app at <https://developer.spotify.com/dashboard> and set its
   redirect URI to `http://127.0.0.1:8000/api/auth/spotify/callback`.
2. Create a Tidal app at <https://developer.tidal.com> and set its redirect URI to
   `http://127.0.0.1:8000/api/auth/tidal/callback`. Request the third-party scopes
   `collection.read`, `collection.write`, `playlists.read`, `playlists.write`,
   `search.read`, and `user.read`.
3. Put `OPE_SPOTIFY_CLIENT_ID`, optional `OPE_SPOTIFY_CLIENT_SECRET`,
   `OPE_TIDAL_CLIENT_ID`, optional `OPE_TIDAL_CLIENT_SECRET`,
   `OPE_YTMUSIC_CLIENT_ID`, `OPE_YTMUSIC_CLIENT_SECRET`, `OPE_SECRET_KEY`, and
   `OPE_FRONTEND_URL` in `.env`.
4. Start Docker Compose, open `http://localhost:8080`, and choose any implemented
   source/target direction advertised by the provider picker.
5. Connect Spotify and Tidal in their OAuth popups.
6. For YouTube Music, open the verification URL shown by the app and enter the
   device code. If Google blocks the unverified OAuth app, or if YouTube Music
   OAuth credentials are not set, use the guided browser-session header fallback
   shown in the connection panel. OAuth reconnects reuse the same YouTube Music
   account by Google email when Google returns it.
7. Pick one playlist, optionally choose individual tracks, and start the migration.
   Tidal **My Collection**, YouTube Music **Liked Songs**, and Spotify **Liked
   Songs** appear as the same `liked_tracks` collection type. Migrating one writes
   directly into the target provider's native liked/saved library instead of
   creating a normal playlist.
   Reconnect older Spotify accounts for `user-library-modify`, and older Tidal
   accounts for `collection.read` and `collection.write`.
   The UI warns before exceeding the safe defaults or before writing into a target
   playlist that has the same name but different songs.
   Spotify may block tracks from playlists you do not own or collaborate on; copy
   those playlists into one you own with Spotify's **Add to other playlist** before
   migrating.
   Spotify playlist lists and selected playlist songs are cached by `snapshot_id`
   to avoid rate limits. Use **Refresh playlists** only when you add playlists or
   need new snapshots, and **Refresh songs** on a playlist only when its songs
   changed.
8. When the job finishes, the progress panel says "Migration succeeded" and links
   to created target playlists when the target provider exposes a web URL.
9. Review low-confidence matches in the progress panel: approve the suggested
   YouTube Music URI, approve all suggested matches, paste a corrected URI/video
   ID, skip one item, or deny all doubtful items.
10. Open the **Stats** tab to inspect one migration from the playlist-name dropdown
    or view all-time aggregate stats filtered by source and target provider. The
    **Migration** tab keeps provider setup, playlist selection, review, and progress
    in a separate workspace.
11. Re-running a playlist reuses an existing migrated target playlist, labels
   partial source playlists/tracks, and skips duplicate target songs with an item
   notice instead of adding them twice.

Detailed Spotify, Tidal, YouTube Music and Apple Music setup steps are in
[`docs/CONNECTING_PROVIDERS.md`](docs/CONNECTING_PROVIDERS.md).

## Adding a provider

Implement `ProviderAdapter` in `backend/app/providers/<name>/adapter.py`, declare a
`CapabilityDescriptor`, `register(...)` it, and pass the conformance suite in
`backend/tests/conformance/`. Adapters only read/search/write — the core
`MatchService` owns matching. Details in [`docs/DESIGN.md`](docs/DESIGN.md) §5.

## License

MIT
