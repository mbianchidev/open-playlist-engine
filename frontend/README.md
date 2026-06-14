# Frontend — Open Playlist Engine

Vite · React 19 · TypeScript. A pure SPA that talks only to the backend's
OpenAPI surface — no shared code with the backend (monorepo, hard-separated).

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
1. Pick source/target providers from `/api/providers`.
2. Connect accounts through generic auth challenges.
3. Load source playlists and optional per-playlist track details from `/api/playlists`.
4. Create a migration with selected playlist and track IDs. Warning popups guard
   slow defaults and same-name target playlist conflicts.
5. Render live job/item progress from SSE.
6. Review low-confidence matches by approving a suggested target URI, pasting a
   replacement URI/video ID, approving all suggested matches, skipping one item, or
   denying all doubtful items.

The current UI supports checked account refresh/test-connection, partial-migration
labels, playlist-level song group selection, and the implemented self-host Spotify
→ YouTube Music MVP.
