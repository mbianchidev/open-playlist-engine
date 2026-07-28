# Frontend — Open Playlist Engine

Vite · React 19 · TypeScript. A pure SPA that talks only to the backend's
OpenAPI surface — no shared code with the backend (monorepo, hard-separated).

The interface uses Lucide for product controls and Simple Icons through
`react-icons` for provider identity. New providers must render through
`ProviderIcon`, which supplies a generic music fallback when no brand mark is
registered.

## Develop
```bash
npm install
npm run dev        # http://localhost:5173 (proxies /api + /health to :8000)
npm run typecheck
npm run build
```

## API types
The checked-in FastAPI contract is `../openapi/open-playlist-engine.json`.
`src/api/schema.d.ts` is generated from it, while `src/api/types.ts` adds the few
frontend-only filter/page shapes:
```bash
npm run gen:api
```
The script invokes a pinned code generator ephemerally, keeping codegen-only parser
dependencies out of the installed/audited frontend dependency graph.

## Flow (maps to the phased design)
1. Pick a source and optional target provider from `/api/providers`.
2. Connect accounts through generic auth challenges.
3. Load source playlists from `/api/playlists` and saved albums/artists from
   `/api/library`, including target limitations and follow/favorite semantics.
4. Without a target account, download selected playlists through `/api/exports` as
   JSON, CSV, TXT, M3U8, or XSPF.
5. Create a migration with selected playlist, track, album, and artist IDs. The
   preflight confirms per-entity counts; warning popups guard slow defaults,
   semantic conversions, and same-name target playlist conflicts.
6. Render live job/item progress from SSE.
7. Review low-confidence matches by approving a suggested target URI, pasting a
   replacement URI/video ID, approving all suggested matches, skipping one item, or
   denying all doubtful items.
8. Reopen migrations in **History**, filter track/album/artist results, inspect prior
   decisions and errors, follow entity-aware target links, and download filtered
   all/problem CSV or JSON reports.
9. Download the source playlist snapshot from a completed/failed migration while its
   retained item details remain available.

The current UI supports checked account refresh/test-connection, partial-migration
labels, playlist-level song group selection, and any provider direction advertised
by backend capabilities, including Spotify ↔ Tidal and YouTube Music ↔ Tidal.
Native liked-track collections map across Spotify Liked Songs, Tidal My Collection,
and YouTube Music Liked Songs. Spotify and Tidal also expose explicit saved-album
and followed/favorite-artist selection. Unsupported target types remain disabled
instead of being represented as playlists.
Apple Music uses the same auth challenge interface with the
official MusicKit JS v3 browser authorization flow.

## Visual system

`src/index.css` retains the established component and state selectors.
`src/theme.css` owns the product tokens, provider-aware presentation, responsive
layout, and motion overrides. Keep dynamic status classes and the ARIA tab
relationships intact when changing presentation.
