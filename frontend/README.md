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
1. Pick source/target providers from `/api/providers`, including the built-in
   local playlist-file source.
2. Connect provider accounts, or upload and validate a local playlist file.
3. Load source playlists from `/api/playlists` and saved albums/artists from
   `/api/library`, including target limitations and follow/favorite semantics, or
   use the normalized preview returned by `/api/imports/preview`.
4. Without a target account, download selected playlists through `/api/exports` as
   JSON, CSV, TXT, M3U8, or XSPF (provider-backed sources only).
5. Create a migration with selected playlist, track, album, and artist IDs. The
   local source remains playlist-only. The preflight confirms per-entity counts;
   warning popups guard slow defaults, semantic conversions, unsupported file
   entries, and same-name target playlist conflicts.
6. Render live job/item progress from SSE.
7. Review low-confidence matches by approving a suggested target URI, pasting a
   replacement URI/video ID, approving all suggested matches, skipping one item, or
   denying all doubtful items.
8. Create and manage recurring rules from completed full-playlist migrations in the
   Sync workspace. The UI exposes add-only/mirror capability gating, cadence/timezone
   editing, run now, pause/resume, delete, changed-track counts, errors and the latest
   migration review panel.
9. Use the Organizer tab to search/sort one provider library, choose a
   capability-gated action, review duplicate evidence, type destructive
   confirmations, and inspect or retry durable per-playlist job results.
10. Reopen migrations in **History**, filter track/album/artist results, inspect prior
   decisions and errors, follow entity-aware target links, and download filtered
   all/problem CSV or JSON reports.
11. Download the source playlist snapshot from a completed/failed migration while its
   retained item details remain available.

The owner-only **Sharing** tab publishes and manages immutable snapshots. Public
`/share/:token` metadata pages redirect to the SPA `/shared/:token` route, where
recipients can inspect tracks, download portable formats, connect only their own
target account, and reuse the same progress/review component through the isolated
public API client.

The current UI supports checked account refresh/test-connection, partial-migration
labels, playlist-level song group selection, and any provider direction advertised
by backend capabilities, including Spotify ↔ Tidal and YouTube Music ↔ Tidal.
Native liked-track collections map across Spotify Liked Songs, Tidal My Collection,
and YouTube Music Liked Songs. Spotify and Tidal also expose explicit saved-album
and followed/favorite-artist selection. Unsupported target types remain disabled
instead of being represented as playlists.
Playlist Organizer defaults to safe library removal, never substitutes deletion,
and requires explicit song-entry selection for playlist edits.
Apple Music uses the same auth challenge interface with the
official MusicKit JS v3 browser authorization flow.
The local-file panel renders detected format/counts, parse findings, duplicates,
unsupported entries, expiry, and playlist/track selection without exposing a
server filesystem path.

## Visual system

`src/index.css` retains the established component and state selectors.
`src/theme.css` owns the product tokens, provider-aware presentation, responsive
layout, and motion overrides. Keep dynamic status classes and the ARIA tab
relationships intact when changing presentation. The accessible workspace tab order
is Migration, Sync, Organizer, History, Sharing.
