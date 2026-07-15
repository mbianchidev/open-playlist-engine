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
`src/api/types.ts` is hand-written for now. Once the backend is running, replace
it with a generated client:
```bash
npm run gen:api    # writes src/api/schema.d.ts from http://localhost:8000/openapi.json
```

## Flow (maps to the phased design)
1. Choose a connected account, public playlist URL, or pasted-text source.
2. Connect required accounts through generic auth challenges.
3. Load account playlists from `/api/playlists`, or preview URL/text input through
   `/api/imports/preview`.
4. Create a migration with selected playlist and track IDs plus either the connected
   source account or persisted import snapshot ID. Warning popups guard
   slow defaults and same-name target playlist conflicts.
5. Render live job/item progress from SSE.
6. Review low-confidence matches by approving a suggested target URI, pasting a
   replacement URI/video ID, approving all suggested matches, skipping one item, or
   denying all doubtful items.

The current UI supports checked account refresh/test-connection, partial-migration
labels, playlist-level song group selection, and any provider direction advertised
by backend capabilities, including Spotify ↔ Tidal and YouTube Music ↔ Tidal.
Native liked-track collections map across Spotify Liked Songs, Tidal My Collection,
and YouTube Music Liked Songs.
Apple Music uses the same auth challenge interface with the
official MusicKit JS v3 browser authorization flow.
URL/text previews show source, owner, track count, unsupported entries, and
line-level parser warnings before the existing track selection UI. Providers that
cannot read a URL publicly return a structured `source_connection_required` action
that exposes only the matching source connection panel.

## Visual system

`src/index.css` retains the established component and state selectors.
`src/theme.css` owns the product tokens, provider-aware presentation, responsive
layout, and motion overrides. Keep dynamic status classes and the ARIA tab
relationships intact when changing presentation.
