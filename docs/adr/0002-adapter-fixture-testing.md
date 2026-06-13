# ADR 0002 — Adapter testing via injected seams + recorded fixtures

Status: Accepted · Date: 2025

Extends ADR 0001 (decision 7, "adapters are search-only") and the DESIGN.md
conformance-suite section. Records how real provider adapters are tested without
hitting live APIs in CI, established while implementing Spotify read/search and
YouTube Music write.

## Context

The contract suite must run against real adapter code, but live provider calls in
CI are non-deterministic, rate-limited, require secrets, and (for unofficial APIs)
carry account-flag risk. Providers also differ: Spotify is a stable, official HTTP
API; YouTube Music is an unofficial library (`ytmusicapi`) whose wire format is not
a stable contract.

## Decisions

1. **One dependency seam per adapter, injected through the constructor.**
   - Spotify takes `transport: httpx.AsyncBaseTransport | None`. Tests pass an
     `httpx.MockTransport` that serves recorded JSON; production passes `None`
     (real network). The full adapter code path runs either way.
   - YouTube Music takes `client_factory: (ProviderCredential) -> YTMusicClient`.
     Tests inject an in-memory fake; production builds a real `ytmusicapi.YTMusic`.
2. **Record HTTP fixtures only for stable, official APIs.** Spotify responses live
   as JSON under `tests/conformance/fixtures/spotify/` and are routed by a small
   path-matching handler. Unofficial APIs (YouTube Music) are faked at the client
   object instead, because their HTTP shape is not a contract worth pinning.
3. **Registered singletons stay real; conformance instantiates classes directly.**
   `register(SpotifyAdapter())` keeps the live default; tests build
   `SpotifyAdapter(transport=...)` / `YTMusicAdapter(client_factory=...)`. No
   registry swapping, no monkeypatching of network internals, no global state.
4. **One parametrized contract suite, scoped per adapter.** `cases.py` declares
   which capabilities each adapter exercises (`reads` / `searches` / `writes`);
   `test_adapter_contract.py` runs the shared behaviours and skips out-of-scope
   capabilities. The fake covers the whole contract; Spotify covers read/search;
   YouTube Music covers write. Adapter-specific edge cases (typed-error mapping,
   ISRC-first search, fidelity flags, videoId parsing, batching) live in
   `test_spotify_adapter.py` / `test_ytmusic_adapter.py`.
5. **Capability descriptors stay honest about intent, not current coverage.** An
   adapter may advertise capabilities whose primitives are still stubbed; the
   stubs raise `NotImplementedError`. The conformance scope — not the descriptor —
   gates which behaviours CI asserts today.

## Consequences

- CI is deterministic and offline; no provider secrets are needed to run tests.
- Adding a provider means: implement the adapter, add a seam, drop in fixtures or a
  fake, and add a case. No new test framework.
- Recorded fixtures can drift from the live API. Mitigation: capability descriptors
  plus (future) opt-in canary tests against live APIs, run out of band — never in
  the default CI path.
