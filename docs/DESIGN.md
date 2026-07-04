# Open Playlist Engine — Design

> Any-to-any playlist migration across music providers (Spotify, YouTube /
> YouTube Music, Tidal, Deezer, Apple Music, …) with a sleek UI and strictly
> separated frontend/backend. This repo is the **first reference implementation**
> of the [`open-playlist`](https://github.com/mbianchidev/open-playlist) spec.

This document is the source of truth for the architecture. It folds in a design
review ("rubber-duck") pass — the notable revisions are called out as **[rev]**.

---

## 1. What this is

`open-playlist` (the existing repo) is **spec-only**: an OpenAPI contract + docs
defining a universal, provider-agnostic `Playlist`/`Track` format with an
ISRC-first matching strategy.

**This repo** is the concrete engine that implements that spec for many providers
and lets a user migrate playlists from any source to any target through a UI.

- Internal interchange model = the spec's `Playlist`/`Track` (`app/core/models.py`).
- Frontend and backend are **hard-separated**: no shared code. The backend is the
  single source of truth and publishes OpenAPI; the frontend consumes a generated
  typed client.

### Goals
- N providers, **any combination** of source → target, both directions.
- Adding a provider is a **plugin drop-in**, not a core change.
- Track matching that gets **cheaper and more accurate over time**.
- Long-running migrations with **durable, replayable live progress**.

### Current implementation status
The self-hosted MVP currently exposes only implemented capabilities in the UI:
Spotify is a source provider (OAuth + playlist read/search) and YouTube Music is a
target provider (header-paste auth + playlist read/search/write through
`ytmusicapi`). The persisted job pipeline supports import → match → write with SSE
item progress; low-confidence matches are marked `needs_review` and can be
approved, batch-approved, corrected, skipped, or batch-denied from the progress
panel.

### Non-goals (for now)
- Streaming/playback. We move playlists, not audio.
- Providers without a usable playlist-write path (e.g. Amazon Music).

---

## 2. Phased flow

Generalized from the original Spotify→YouTube framing to any source/target.
**[rev]** the pipeline is explicitly ordered **import → match → review → write**
so matching is decoupled from writing and the user reviews before anything is
created.

| Phase | Step | Component |
|---|---|---|
| 0 | Get access to **source** | Backend auth (per-provider strategy) |
| 1 | **Import**: fetch playlists from source (names + tracks, capture ISRC) | Source adapter → Open Playlist model |
| 2 | UI: select/deselect playlists and songs | Frontend selection tree |
| 3 | Get access to **target** | Backend auth |
| 3.5 | **Match**: resolve each track on the target | `MatchService` (graph → search → score) |
| 3.6 | **Review**: confirm/fix low-confidence matches | Frontend review queue |
| 4 | **Write**: create playlists and add tracks, idempotently | arq job + operation ledger |
| 5 | UI shows live progress | Frontend SSE progress board |

### Safe migration defaults
Spotify → YouTube Music conversion is deliberately boring and slow by default:
1 playlist per job, 50 selected tracks per job, 250 target tracks per day, and 120
seconds between jobs. `POST /api/migrations` performs the preflight and returns a
409 warning payload when a job exceeds those defaults or when a same-name target
playlist has completely different songs. The frontend shows a confirmation popup
and resubmits with `acknowledge_warnings=true` only after user acknowledgement.

### Partial reruns and duplicate handling
Completed `job_item` rows are the private migration ledger. Playlist and track read
responses can include migration status when the frontend supplies target context,
so a rerun can label a source playlist as partially migrated and mark leftover
songs. The worker reuses a previously observed target playlist, or a same-name
target playlist whose songs overlap, and skips duplicate target songs with a
per-item reason instead of adding them twice.

---

## 3. Architecture

### Hub-and-spoke (O(N), not O(N²))
Every provider is a spoke; the universal Open Playlist format is the hub. Migration
is `source.read() → OpenPlaylist → target.write()`. Add a provider once and it
works with all others, both directions.

```
Spotify ┐                                  ┌ YouTube / YT Music
Tidal   ┼─ read → [ OPEN PLAYLIST hub ] → write ─┼ Tidal
Deezer  ┤            (identity graph)           ├ Deezer
Apple   ┘                                  └ Apple
```

### Frontend / backend separation
- **Backend** owns all OAuth/tokens, provider API calls, matching, jobs,
  orchestration. Emits OpenAPI.
- **Frontend** owns the source→target wizard, selection, review, progress. It
  consumes a client **generated from the backend OpenAPI**. No business logic, no
  provider secrets.

### Deployment model — **[rev]**
v1 targets **self-hosted, single-user**, but every multi-tenant seam is present so
the same codebase can run **hosted**. A single `OPE_DEPLOYMENT_MODE`
(`self_host` | `hosted`) flag drives the differences, and secret handling goes
through a pluggable `KeyProvider` (env-derived Fernet now; KMS later). Examples:
- header/cookie-paste auth is allowed **only** in self-host (`allow_header_paste`).
- the shared match graph stays local unless explicitly enabled.

---

## 4. Tech stack

- **Backend**: Python 3.12, **FastAPI**, SQLAlchemy 2 (async) + Alembic, **arq**
  (async jobs on Valkey), Pydantic v2 mirroring the Open Playlist schema.
- **Frontend**: **Vite + React + TypeScript**, typed client generated from the
  backend OpenAPI, **SSE** for progress.
- **Data**: **Postgres** (accounts, encrypted credentials, jobs, identity graph),
  **Valkey** (job queue + pacing).
- **Infra**: `docker compose` (backend, worker, frontend, postgres, valkey), built
  with `--no-cache`.

### YouTube write path
- **Default: `ytmusicapi`** (unofficial) — real YouTube Music, **no quota**,
  actively maintained. Enabled by default; marked EXPERIMENTAL.
- **Optional: official YouTube Data API v3** — clean OAuth but **~66 songs/day** on
  the default 10k quota (`search.list`=100, `playlistItems.insert`=50). **Off by
  default**, behind a flag.

---

## 5. Provider plugin contract

A provider implements `ProviderAdapter` (`app/core/adapter.py`), declares a
`CapabilityDescriptor`, and registers itself.

### **[rev]** Adapters do not own matching
Adapters expose only read/search/write primitives. They **never** read or write the
identity graph — the core `MatchService` owns caching, scoring and promotion. This
keeps a bad match in one context from silently becoming global truth.

```python
class ProviderAdapter(Protocol):
    info: ProviderInfo
    auth: AuthStrategy

    # READ (async + paginated)
    def iter_playlists(self, cred) -> AsyncIterator[PlaylistRef]: ...
    def iter_playlist_items(self, cred, ref) -> AsyncIterator[Track]: ...
    async def read_playlist(self, cred, ref) -> Playlist: ...
    async def test_connection(self, cred) -> None: ...

    # SEARCH (used by MatchService; returns candidates, scores nothing)
    async def search_tracks(self, cred, track, *, limit=5) -> list[TrackCandidate]: ...
    async def validate_uri(self, cred, uri) -> bool: ...

    # WRITE (idempotency handled by the core operation ledger; per-item results)
    async def create_playlist(self, cred, spec) -> str: ...
    async def add_tracks(self, cred, playlist_id, uris) -> list[AddItemResult]: ...
```

### Registration & trust boundary — **[rev]**
- Adapters self-register via `app.core.registry.register(...)`; third parties can
  ship adapters as `importlib.metadata` entry points (group `ope.providers`).
- **Hosted** mode runs an **allow-list** of signed/vetted plugins. **Self-host**
  trusts locally installed modules. The registry is the choke point.

### Contract rules every adapter MUST honor
1. Map **to/from the Open Playlist model only** — never leak provider types.
2. **Populate ISRC on read** when available; set `provider_uris[self.name]`.
3. **Search only** — return `TrackCandidate`s; never touch the graph.
4. Writes are **replayable** via the operation ledger (see §9) — no "dedupe by
   name" guessing. **[rev]**
5. **Raise typed errors** (`RateLimited`, `AuthExpired`, `NotFound`, `Unsupported`)
   — never leak HTTP. Pacing is centralized (see §9), not per-adapter. **[rev]**
6. **Capability honesty**: advertise only what's implemented and tested.
7. **No global state**; everything flows through `cred` → multi-account safe.

### Fidelity contract — **[rev]**
v1 migrates **flat, music-track playlists**. Non-songs (podcast episodes, videos,
local files) and folders are **not** silently dropped: each carries an
`unsupported_reason` and is surfaced in a per-job **lossy report**. `Track`
exposes `media_type`, `is_local`, `position` and `is_migratable` for this.

### Conformance suite — **[rev]**
Core ships `tests/conformance/` with a fake in-memory provider and a contract test
suite (protocol satisfied, read round-trip preserves ISRC, search returns
candidates, create→add reports per-item results, typed errors). Real adapters
parametrize the same suite against **recorded fixtures / canaries — never live APIs
in CI**.

---

## 6. Auth abstraction

Providers differ wildly; we collapse them into a few strategy kinds, each with one
lifecycle, so the frontend needs only **three** generic "connect" UIs.

```python
class AuthStrategy(Protocol):
    kind: AuthKind  # OAUTH_PKCE | OAUTH_DEVICE | HEADER_PASTE | DEVELOPER_USER_TOKEN | LONG_LIVED_TOKEN
    async def begin(self, *, user_id, account_label=None) -> AuthChallenge: ...
    async def complete(self, *, user_id, callback) -> ProviderCredential: ...
    async def refresh(self, cred) -> ProviderCredential: ...
    async def revoke(self, cred) -> None: ...
```

### Three challenge shapes the frontend renders
1. **`redirect`** (OAuth Auth-Code + PKCE) → Spotify, Tidal, YouTube official, Deezer.
2. **`device_code`** → YouTube Music (`ytmusicapi` OAuth, "TV & Limited Input" client).
3. **`form`** (schema-driven) → header paste (self-host only), Apple Music user token.

> N providers collapse into 3 UX patterns.

### **[rev]** Provider-specific lifecycle hooks & multi-account
Some providers don't fit a plain redirect — **Apple Music MusicKit** needs a
first-class developer-token + client-fetched user-token flow, so `AuthStrategy`
allows per-provider `begin/complete` shapes rather than one hardcoded OAuth dance.
Credentials are keyed by **`provider_account_id` + credential `version` + granted
`scopes`**, so a user can connect multiple accounts of the same provider and we can
re-auth for new scopes without losing history. Header-paste is **prohibited in
hosted mode**.

### Per-provider reality (verify at build time)
- **Spotify** — OAuth PKCE; refresh tokens; ISRC-rich. Clean.
- **YouTube official** — Google OAuth; refresh tokens; quota-limited.
- **YouTube Music (`ytmusicapi`)** — unofficial; device-code OAuth or header paste;
  **no ISRC** → text search + graph + review.
- **Tidal** — OAuth PKCE; ISRC available.
- **Deezer** — OAuth; write needs app approval (tightening).
- **Apple Music** — MusicKit dev token (ES256 JWT, paid acct) + client user token.
- **Amazon Music** — no public playlist write. Out of scope.

### Credential storage
Encrypted blob (Fernet via `KeyProvider`), `auth_kind`, `scopes`, `expires_at`,
`refresh_token`, `version`, `account_label`. Refresh ahead of expiry. Never log
tokens; redact in errors; PKCE + `state`; minimal scopes per capability.

---

## 7. Capability matrix — **[rev]** descriptors, not booleans

Adapters advertise a structured `CapabilityDescriptor`, because the UI and the
scheduler need **constraints**, not just "can write":

- capability set: `READ_PLAYLISTS/TRACKS/LIBRARY`, `CREATE_PLAYLIST`, `ADD_TRACKS`,
  `REMOVE_TRACKS`, `REORDER`, `SET_COVER`, `SET_DESCRIPTION`
- `has_isrc`, `search_modes` (`isrc`/`text`), `official`, `stability`
- write constraints: `max_add_batch`, `max_playlist_size`, `supports_duplicates`,
  `ordering` (`preserved`/`best_effort`/`none`), `description_max_len`
- pacing/cost: `search_quota_cost`, `write_quota_cost`, `daily_quota`
- `warning` (free-form caveat surfaced in the UI)

### Honest matrix (verify per provider)
| Provider | Read | Write | ISRC | Target lookup | Auth | Notes |
|---|---|---|---|---|---|---|
| Spotify | ✓ | ✓ | ✓ | ISRC + text | OAuth PKCE | official, solid |
| YT Music (`ytmusicapi`) | ✓ | ✓ | ✗ | text only | device/header | unofficial, no quota, no ISRC |
| YouTube (Data API) | ✓ | ✓ | ✗ | text (quota) | OAuth | official, ~66 songs/day |
| Tidal | ✓ | ✓ | ✓ | ISRC + text | OAuth PKCE | official dev portal |
| Deezer | ✓ | ~ | ✓ | ISRC + text | OAuth | write needs approval |
| Apple Music | ✓ | ✓ | ✓ | ISRC + text | MusicKit | heaviest auth, paid acct |
| Amazon Music | ✗ | ✗ | — | — | — | no public write |

### UI consequences
`GET /providers` returns the matrix; the FE renders source/target pickers and
inline warnings dynamically, so new plugins appear automatically. Core gate before
a job: source `READ_TRACKS` ∧ target `CREATE_PLAYLIST` + `ADD_TRACKS`. Missing
optional caps → skip + warn, never hard-fail.

---

## 8. Identity / evidence graph — **[rev]**

A provider-agnostic track identity map that grows with every migration — but
modeled as an **evidence/candidate graph keyed by an internal `track_identity`
UUID**, *not* by ISRC as the primary key. ISRC is strong evidence, not identity.

```
track_identity(id UUID pk, isrc?, title, artist, album, duration_s)
track_edge(
  identity_id -> track_identity,
  provider, provider_track_id, provider_uri,
  confidence, source,            -- isrc_exact | fuzzy | user_confirmed
  scope,                         -- 'global' | 'account:<id>'   (overlay)
  created_at
)
```

### Why a graph, not an ISRC table
- One ISRC can map to several provider tracks (studio/live/clean/explicit/region);
  conversely fuzzy links are uncertain. A graph holds **candidates with evidence**.
- **Per-user confirmations are overlays** (`scope = account:<id>`): a user fix
  applies to *their* migrations immediately. It is **promoted to `global` only on
  strong, corroborated evidence** — a single fuzzy/user guess never becomes global
  truth. This is the safety the review flagged.

### How it self-enriches
1. **Read**: a track with ISRC + id records a high-confidence edge for free.
2. **Write**: to place a track on a no-ISRC target, `MatchService` checks the graph
   first (cache hit → zero searches → saves quota), else asks the adapter to search,
   scores candidates locally, and records the chosen edge.
3. **Review**: a user fix writes a `user_confirmed` overlay edge.
4. **Reverse bridging**: once a no-ISRC `provider_track_id` links to an identity,
   the reverse direction resolves too.

### Privacy
The **global** graph holds no PII and is potentially shareable; **per-account
overlays and all playlist/selection/job data are private**. Sharing the global
graph as an open dataset is deferred pending legal review.

---

## 9. Migration job, idempotency & progress

- `migration_job` + `job_item` per song (status: pending/matched/needs_review/
  written/skipped/failed). Runs on an **arq** worker.
- **[rev] Real idempotency via an operation ledger.** Instead of "dedupe by name",
  each write records **intent → call → observed target id/position**. On an
  uncertain failure we **reconcile by reading target state**, never blindly retry a
  non-idempotent insert. `operation_ledger` persists this.
- **[rev] Central rate limiter** (`app/core/rate_limit.py`, token bucket) paces all
  providers using the capability cost hints — not per-adapter sleeps — with jitter
  for unofficial providers to avoid account flags.
- Spotify read calls cache `/me/playlists` results and selected playlist tracks by
  `snapshot_id`. The UI does not automatically refresh Spotify lists on every app
  load; users refresh playlist refs or songs explicitly when they need new data.
- **[rev] Durable, replayable progress.** Progress is derived from persisted
  `job_item` rows and streamed over **SSE**; a reconnecting client resumes via
  `Last-Event-ID`, so no events are lost on a dropped connection.

---

## 10. Data model summary

| Table | Purpose | Scope |
|---|---|---|
| `provider_account` | a connected account (provider + label) | private |
| `provider_credential` | encrypted tokens, auth_kind, scopes, expiry, version | private |
| `migration_job`, `job_item` | jobs + per-song status (drives progress) | private |
| `operation_ledger` | intent vs observed writes (idempotency) | private |
| `track_identity` | canonical track (UUID pk, ISRC as evidence) | global, no PII |
| `track_edge` | provider links with confidence/source/scope | global + per-account overlays |

---

## 11. Repo layout (actual)

```
open-playlist-engine/
  backend/
    app/
      core/        # models, capabilities, adapter contract, registry,
                   # match_service, rate_limit, security  (provider-agnostic hub)
      providers/   # spotify/, ytmusic/  (self-registering adapters)
      db/          # SQLAlchemy models (private data + identity graph)
      jobs/        # arq worker + import→match→review→write pipeline
      api/         # FastAPI routers: /providers /auth /playlists /migrations
    tests/conformance/   # fake provider + contract suite
    migrations/          # Alembic
  frontend/        # Vite + React + TS SPA (consumes generated OpenAPI client)
  docs/            # this doc + ADRs
  docker-compose.yml     # backend, worker, frontend, postgres, valkey
```

Frontend and backend stay strictly separate: no shared code, FE consumes only the
generated OpenAPI client.

---

## 12. Security & privacy

- Encrypt provider credentials at rest via `KeyProvider`; never log/redact tokens.
- PKCE + `state`; refresh ahead of expiry; minimal scopes per capability.
- Separate the PII-free global graph from private user data and per-account overlays.
- Unofficial adapters: pace + jitter; surface "may break / ToS-grey" warnings.
- Header-paste auth disabled in hosted mode; plugin allow-list in hosted mode.

---

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| YouTube official quota (~66 songs/day) | `ytmusicapi` default; official off; graph cache cuts searches |
| Unofficial API breakage / account flags | central pacing + backoff + jitter; typed errors; official fallback |
| Awkward auth (ytmusic, Apple) | 3 challenge shapes + per-provider lifecycle hooks |
| Fuzzy mismatches | ISRC-first; review step; confirmations as overlays, promoted only on evidence |
| Partial writes on retry | operation ledger: reconcile by reading target state |
| Provider API/ToS changes | capability descriptors + conformance suite catch regressions |
| Lossy migrations | fidelity contract + per-job lossy report (`unsupported_reason`) |

---

## 14. MVP build order

1. **Backend**: Spotify OAuth (PKCE) + import → Open Playlist. (Phases 0–1)
2. **Frontend**: connect Spotify + selection tree. (Phase 2)
3. **Backend**: `ytmusicapi` writer + `MatchService` + identity graph + arq job +
   operation ledger. (Phases 3–4)
4. **Frontend**: review queue + SSE progress board. (Phases 3.6, 5)
5. **Add**: official YouTube writer behind flag.
6. **Then**: each new provider (Tidal → Deezer → Apple) is a plugin + conformance pass.

---

## 15. Decisions log

- ✅ Reference implementation of the `open-playlist` spec; reuse `Playlist`/`Track`.
- ✅ Any-to-any via hub-and-spoke (O(N) adapters).
- ✅ Monorepo, **hard-separated** FE/BE; FE consumes generated OpenAPI client.
- ✅ Stack: Python/FastAPI + arq, Vite/React/TS, Postgres, Valkey, docker compose.
- ✅ `ytmusicapi` default-on; official YouTube Data API opt-in/off.
- ✅ Pipeline ordered import → match → review → write.
- ✅ **[rev]** adapters search-only; `MatchService` owns matching.
- ✅ **[rev]** evidence graph keyed by UUID; per-account overlays; evidence-gated promotion.
- ✅ **[rev]** idempotency via operation ledger; central rate limiter; replayable SSE.
- ✅ **[rev]** capability descriptors (constraints); fidelity contract + lossy report.
- ✅ Self-host single-user v1 with SaaS-ready seams (`DEPLOYMENT_MODE` + `KeyProvider`).

## 16. Open questions

- Sharing the global graph as an open dataset — needs legal review.
- Provider priority after Spotify + YT Music (Tidal looks lowest-friction).
- When to split into two repos (if ever) — current hard separation keeps it cheap.
