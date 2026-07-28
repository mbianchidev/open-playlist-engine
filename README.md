# Open Playlist Engine

Any-to-any music **playlist and library migration plus portable export** — move
playlists, liked tracks, saved albums, and followed/favorite artists between
supported providers, import local playlist files, or download playlists as
portable local files.

This is the first reference implementation of the
[`open-playlist`](https://github.com/mbianchidev/open-playlist) universal
`Playlist`/`Track` spec. Architecture is **hub-and-spoke**: every provider is a
plugin spoke, the universal format is the hub, so adding a provider is O(1) and it
instantly works with all the others — both as source and target.

> Status: **early MVP**. Spotify, Tidal, YouTube Music and Apple Music advertise
> implemented directions dynamically: Spotify OAuth/read/search/write, Tidal
> OAuth/read/search/write, YouTube Music device/header auth/read/search/write, and
> official Apple MusicKit library read/search/write. Persisted credentials,
> playlist/track/album/artist selection, CSV/TXT/M3U8/XSPF/JSON playlist exports,
> partial-migration detection, migration jobs, review actions, SSE progress,
> reopenable migration history, mixed-entity statistics, streamed CSV/JSON
> reports, opt-in immutable public playlist shares, and a capability-gated Playlist
> Organizer are wired. Local TXT, CSV, M3U/M3U8, PLS, WPL, XSPF, XML, and JSON
> playlist sources are built in. Other provider
> directions remain gated until
> their adapters advertise implemented capabilities. Persistent scheduled sync
> rules can keep completed single-playlist migrations updated in add-only mode,
> with capability-gated mirror mode for Spotify targets. See
> [`docs/DESIGN.md`](docs/DESIGN.md).

## How it works

```
provider or local file ─▶ [ Open Playlist hub ] ─ write ─▶ target provider
                              │ (identity graph)
                              └─ export ─▶ local CSV/TXT/M3U8/XSPF/JSON
```

Pipeline: **import → match → review → write**, with durable, replayable progress.
Matching is ISRC-first with a self-enriching evidence graph and a human review step
for low-confidence matches. Scheduled rules reuse the same pipeline and operation
ledger instead of maintaining a second migration engine.

## Layout

| Path | What |
|---|---|
| `backend/` | FastAPI app, provider adapters, matching, jobs, DB. See [`backend/README.md`](backend/README.md). |
| `frontend/` | Vite + React SPA, consumes the backend OpenAPI. See [`frontend/README.md`](frontend/README.md). |
| `openapi/` | Vendored [`open-playlist`](https://github.com/mbianchidev/open-playlist) spec the universal `Playlist`/`Track` model mirrors. |
| `docs/` | Design, local imports, provider setup, sync, Organizer, sharing, history, portable exports, and ADRs. |

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
`OPE_EXPORT_MAX_PLAYLISTS`, `OPE_SECRET_KEY`, `OPE_FRONTEND_URL`, and
`OPE_MIGRATION_HISTORY_RETENTION_DAYS`. Public playlist sharing additionally uses
`OPE_PUBLIC_BASE_URL` and `OPE_OWNER_ACCESS_TOKEN`; it remains disabled while the
public URL is empty. Local imports use the `OPE_LOCAL_IMPORT_*` size, item-count,
spool, lease, and retention controls.
Self-host mode resolves the migration owner server-side as the local user. Hosted
mode fails closed until a real user-authentication dependency is configured; it
does not accept a caller-provided user ID.
Safe migration defaults are intentionally slow and can be overridden only after a
warning in the UI: 1 playlist/job, 50 tracks/job, 250 tracks/day, and 120 seconds
between jobs (`OPE_MIGRATION_SAFE_*`). Worker jobs can run for up to 3600 seconds
by default (`OPE_MIGRATION_WORKER_JOB_TIMEOUT_S`) so large playlists do not hit
ARQ's 5-minute default timeout.
The existing worker also evaluates persisted sync schedules at startup and every
minute. Sync cadence bounds, retry delay, stale-run recovery and scheduler batch
size use the `OPE_SYNC_*` settings shown in [`.env.example`](.env.example).
Organizer pacing and retries use `OPE_ORGANIZER_RATE_LIMIT_CAPACITY`,
`OPE_ORGANIZER_RATE_LIMIT_REFILL_PER_S`, `OPE_ORGANIZER_RETRY_ATTEMPTS`,
`OPE_ORGANIZER_RETRY_MAX_DELAY_S`, and
`OPE_ORGANIZER_WORKER_JOB_TIMEOUT_S`.
Portable exports allow up to 100 playlists per
download by default (`OPE_EXPORT_MAX_PLAYLISTS`) and do not impose a track cap.

## Portable local exports

Connect a source account, select one or more playlists or individual tracks, choose
a file format, then use **Download export**. A target provider is not required.
The **History** tab can also download the source playlist snapshot recorded by a
completed or failed migration while its retained item details remain available.

Single-playlist exports download directly. Multi-playlist exports use a deterministic
ZIP with `manifest.json`; JSON archives contain one lossless, versioned Open Playlist
bundle, while CSV, TXT, M3U8, and XSPF archives contain one collision-safe file per
playlist. Output is generated through temporary files and streamed to the browser,
then deleted after completion or cancellation.

Format schemas, MIME types, encodings, warning behavior, filenames, limits, and API
examples are documented in
[`docs/EXPORTING_PLAYLISTS.md`](docs/EXPORTING_PLAYLISTS.md).

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

## Local playlist files

Choose **Local playlist file** as the source to upload TXT, CSV, M3U, M3U8, PLS,
WPL, XSPF, XML, or JSON. The app streams the request through configurable size
and item limits, handles UTF BOMs and documented fallback encodings, previews
malformed rows, duplicates, and unsupported local audio entries, then migrates
selected tracks through the same match/review/write pipeline.

Raw files are closed immediately after parsing and are never sent to an external
service. Only the normalized preview is retained temporarily in Postgres; unused
previews expire, successful jobs delete them, and failed jobs keep a short retry
grace. Canonical CSV headers, accepted aliases, format behavior, limits, API
usage, and retention rules are documented in
[`docs/LOCAL_FILE_IMPORTS.md`](docs/LOCAL_FILE_IMPORTS.md).

## Spotify, Tidal, YouTube Music and Apple Music

| Provider | Playlists / liked tracks | Saved albums | Followed/favorite artists |
|---|---|---|---|
| Spotify | Read/write | Read/write | Read/write as follows |
| Tidal | Read/write | Read/write | Read/write as favorites |
| YouTube Music | Read/write | Unsupported | Unsupported |
| Apple Music | Read/write | Unsupported in this implementation | Unsupported |

Album/artist selections are shown only when the source exposes them. Target
limitations remain visible and disabled; the engine never converts unsupported
albums or artists into synthetic playlists.

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
7. Pick playlists, optionally choose individual tracks, and select supported saved
   albums or followed/favorite artists. The preflight shows counts for every entity
   type before starting.
   Tidal **My Collection**, YouTube Music **Liked Songs**, and Spotify **Liked
   Songs** appear as the same `liked_tracks` collection type. Migrating one writes
   directly into the target provider's native liked/saved library instead of
   creating a normal playlist.
   Reconnect older Spotify accounts for `user-library-modify`, `user-follow-read`,
   and `user-follow-modify`, and older Tidal
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
10. Open **Organizer** to filter and sort one connected library, safely remove
    playlists, permanently delete owned playlists where supported, or remove exact
    song entries. The preflight shows ownership, collaboration, recovery impact, and
    unsupported operations. Destructive work requires an exact typed phrase; retries
    run failed playlist items only. Duplicate scans are review-only and never select
    or remove a candidate. See
    [`docs/PLAYLIST_ORGANIZER.md`](docs/PLAYLIST_ORGANIZER.md).
11. Open the **History** tab to reopen completed, partial, or failed migrations.
    Inspect accounts, collections, lifecycle timestamps, warnings, target links,
    prior review decisions, and filtered item results; download all rows or only
    problem rows as streamed CSV/JSON. Aggregate stats remain filterable by source
    and target provider. Report fields and retention behavior are documented in
    [`docs/MIGRATION_HISTORY.md`](docs/MIGRATION_HISTORY.md).
12. Re-running a playlist reuses an existing migrated target playlist, labels
   partial source playlists/tracks, and skips duplicate target songs with an item
   notice instead of adding them twice. Saved albums and artists use native target
   contains checks before writes, so reruns report already-present items instead of
   issuing duplicate actions. Name-only artist matches always require review.
13. Open the **Sync** tab after a completed full-playlist migration to create a
   recurring rule. Choose add-only or an available mirror mode, cadence and IANA
   timezone; then run now, pause/resume, edit, delete or inspect the latest result.
   Rules and checkpoints survive restarts. A rule waits when tracks need review
   and resumes its schedule after those items are resolved.

Detailed Spotify, Tidal, YouTube Music and Apple Music setup steps are in
[`docs/CONNECTING_PROVIDERS.md`](docs/CONNECTING_PROVIDERS.md).
Scheduled synchronization behavior and recovery details are in
[`docs/SYNCHRONIZATION.md`](docs/SYNCHRONIZATION.md).

## Adding a provider

Implement `ProviderAdapter` in `backend/app/providers/<name>/adapter.py`, declare a
`CapabilityDescriptor`, `register(...)` it, and pass the conformance suite in
`backend/tests/conformance/`. Adapters only read/search/write — the core
`MatchService` owns matching. Details in [`docs/DESIGN.md`](docs/DESIGN.md) §5.

## License

MIT
