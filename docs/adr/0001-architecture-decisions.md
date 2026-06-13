# ADR 0001 — Initial architecture decisions

Status: Accepted · Date: 2025

Context and full rationale live in [`../DESIGN.md`](../DESIGN.md). This record is
the short list of binding decisions for the scaffold.

## Decisions

1. **Reference implementation of the `open-playlist` spec.** The universal
   `Playlist`/`Track` model is the internal hub format.
2. **Any-to-any, hub-and-spoke.** Providers are spokes; the hub is the universal
   format. O(N) adapters, not O(N²) pairs.
3. **Monorepo, hard-separated FE/BE.** No shared code. The frontend consumes a
   client generated from the backend's OpenAPI document only.
4. **Stack.** Backend: Python 3.12 / FastAPI / SQLAlchemy 2 async / Alembic / arq.
   Frontend: Vite / React / TypeScript. Data: Postgres + Valkey. Infra: docker
   compose.
5. **YouTube write path.** `ytmusicapi` (unofficial) default-on; official YouTube
   Data API v3 opt-in and off by default (quota ~66 songs/day).
6. **Pipeline order: import → match → review → write.**
7. **Adapters are search-only.** The core `MatchService` owns caching, scoring and
   promotion; adapters never touch the identity graph.
8. **Identity graph is an evidence graph keyed by an internal UUID.** ISRC is
   evidence, not identity. User confirmations are per-account overlays, promoted to
   global only on strong evidence.
9. **Idempotency via an operation ledger.** Persist intent → call → observed state;
   reconcile by reading target state instead of blind retries.
10. **Central rate limiter** (token bucket) using capability cost hints; not
    per-adapter sleeps.
11. **Durable, replayable progress.** Derived from `job_item`, streamed over SSE
    with `Last-Event-ID` resume.
12. **Capability descriptors, not booleans.** Adapters advertise constraints (batch
    size, ordering, quota, stability, warnings).
13. **Fidelity contract.** Flat music-track playlists; unsupported items carry an
    `unsupported_reason` and appear in a per-job lossy report.
14. **Deployment: self-host single-user v1 with SaaS-ready seams.** One
    `OPE_DEPLOYMENT_MODE` flag + pluggable `KeyProvider`; header-paste auth and
    untrusted plugins are self-host-only.
