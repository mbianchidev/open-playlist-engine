# Connecting Spotify, Tidal, Apple Music and YouTube Music

This self-hosted MVP supports capability-driven migration across Spotify, Tidal,
Apple Music and YouTube Music. Keep `.env` local: it contains provider secrets and
session tokens.

## Spotify app setup

1. Open <https://developer.spotify.com/dashboard>.
2. Click **Create app**.
3. Use any app name and description, for example `Open Playlist Engine Local`.
4. Set **Redirect URI** exactly to:

   ```text
   http://127.0.0.1:8000/api/auth/spotify/callback
   ```

   Spotify rejects `localhost`; use the explicit loopback address.
5. Select the required APIs/products if Spotify asks. The app uses the Web API.
6. Save the app, then open **Settings**.
7. Copy **Client ID** and **Client secret** into the repo-root `.env`:

   ```env
   OPE_SPOTIFY_CLIENT_ID=your_client_id
   OPE_SPOTIFY_CLIENT_SECRET=your_client_secret
   OPE_SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/api/auth/spotify/callback
   ```

8. If the Spotify app is still in development mode, add your Spotify account email
   under **User Management**.
9. Restart the backend and worker:

   ```bash
   docker compose up -d --force-recreate backend worker
   ```

10. In the UI, choose Spotify as **From** or **To**, click **Connect Spotify**, and approve
    the requested scopes.
11. Spotify **Liked Songs** is shown as an owned playlist and uses Spotify's
    saved library. Reconnect accounts created before library writes so the app can
    request both `user-library-read` and `user-library-modify`.
12. Use **Test connection** after connecting. Spotify refresh tokens expire after
    six months; when Spotify returns `invalid_grant`, the app discards the stale
    account before asking you to reconnect. **Refresh accounts** also removes stale
    accounts so you can reconnect cleanly.

### Spotify playlists owned by someone else

Spotify blocks the playlist-items endpoint for playlists where the signed-in user
is neither the owner nor a collaborator. If loading tracks or starting migration
returns a Spotify access error, open the playlist in Spotify and use **Add to other
playlist** to copy it into a playlist you own, then migrate that copy. Delta
migration is not available for the original external playlist because Spotify does
not let the app read its tracks.

### Spotify rate limits and cache

Spotify can return long `Retry-After` windows. To reduce calls, the app caches
Spotify playlist refs with their `snapshot_id` and caches each selected playlist's
tracks for that snapshot. Normal app refreshes use the cache. Use **Refresh
playlists** only after adding or changing playlists so the app can discover new
snapshot IDs. Use **Refresh songs** inside a playlist only when you need to force a
track refresh; otherwise cached songs are reused until the playlist snapshot
changes.

### Spotify organizer behavior

The Organizer's default **Remove from library** action uses Spotify's generic
library removal endpoint and is non-destructive: the playlist can be followed again
while it remains available. Spotify does not expose permanent playlist deletion, so
that mode is never offered.

**Remove songs** is available only for owned or collaborative playlists. It sends
the exact selected positions with the current `snapshot_id` and is limited to 100
entries per job. The limit prevents position drift across multiple Spotify
snapshots. If the playlist changes after preflight, the job fails that playlist and
asks for a refresh instead of removing a different occurrence.

## Tidal app setup

Tidal uses the official TIDAL Web API with OAuth Authorization Code + PKCE.

1. Open <https://developer.tidal.com>.
2. Create an app for local development.
3. Set **Redirect URI** exactly to:

   ```text
   http://127.0.0.1:8000/api/auth/tidal/callback
   ```

4. Request these third-party scopes:

   ```text
   collection.read
   collection.write
   playlists.read
   playlists.write
   search.read
   user.read
   ```

   Do not request the internal-only `r_usr` or `w_usr` scopes for third-party apps.
5. Copy the client ID and, if issued, the client secret into the repo-root `.env`:

   ```env
   OPE_TIDAL_CLIENT_ID=your_client_id
   OPE_TIDAL_CLIENT_SECRET=your_client_secret
   OPE_TIDAL_REDIRECT_URI=http://127.0.0.1:8000/api/auth/tidal/callback
   ```

   `OPE_TIDAL_CLIENT_SECRET` is optional for PKCE sign-in, but set it when
   available. The adapter uses client credentials for catalog ISRC lookups and
   batched track metadata hydration. Without a secret it falls back to text search
   for target matching and scoped single-track detail requests when reading
   playlists.

6. Restart the backend and worker:

   ```bash
   docker compose up -d --force-recreate backend worker
   ```

7. In the UI, choose Tidal as **From** or **To**, click **Connect Tidal**, and
   approve the requested scopes in the popup.

Tidal playlist writes create `UNLISTED` playlists by default unless a migration
explicitly asks for a public playlist. The adapter writes tracks in batches of 50,
the maximum accepted by Tidal's playlist-items and My Collection endpoints. Tidal
**My Collection** is exposed as a liked-tracks collection.

The Organizer can permanently delete playlists returned by Tidal's owner-filtered
listing after typed confirmation. Tidal has no verified safe unfollow operation in
this adapter. Song removal stays disabled because the public duplicate-occurrence
semantics cannot yet be guaranteed.

## Apple MusicKit setup

Apple Music does not provide an OAuth client ID/client secret flow. The official
integration uses two tokens:

- a developer JWT signed by the backend with your Apple Developer Team ID, MusicKit
  Key ID and `.p8` private key;
- a Music User Token issued interactively by MusicKit JS after the user signs in.

An active Apple Developer Program membership and Apple Music subscription are
required.

1. Create a MusicKit identifier and private key by following Apple's
   [Media identifier and private key guide](https://developer.apple.com/help/account/capabilities/create-a-media-identifier-and-private-key/).
2. Download the `.p8` key once and record its 10-character Key ID.
3. Find your 10-character Team ID in the Apple Developer membership page.
4. Configure the repo-root `.env`:

   ```env
   OPE_APPLE_MUSIC_TEAM_ID=YOURTEAMID
   OPE_APPLE_MUSIC_KEY_ID=YOURKEYID
   OPE_APPLE_MUSIC_PRIVATE_KEY_PATH=/absolute/path/to/AuthKey_YOURKEYID.p8
   OPE_APPLE_MUSIC_TOKEN_TTL_S=86400
   ```

   The key path must be readable by both the backend and worker. For Docker,
   either mount the key at the same container path or store an escaped PEM value:

   ```env
   OPE_APPLE_MUSIC_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
   ```

5. Restart the backend and worker.
6. Choose Apple Music as **From** or **To**, click **Connect Apple Music**, then
   click **Authorize with Apple Music** in the connection panel.
7. Sign in to Apple and approve library access. The browser receives a Music User
   Token and sends it to the backend, where it is stored encrypted.
8. Use **Test connection** before migrating.

Apple library tracks use user-specific IDs, while migration writes require catalog
song IDs. The adapter maps `playParams.catalogId`, enriches tracks through the
user's storefront for ISRC matching, and writes catalog songs directly. Uploaded
or matched tracks without a catalog ID remain usable as source metadata but cannot
be written back by library ID.

Apple documents propagation delays after playlist creation and track additions.
The adapter retries a not-yet-visible playlist only immediately after creating it;
later writes fail normally instead of masking a bad playlist ID. New playlists or
tracks may still take time to appear in the Apple Music app.

Apple Music remains read-only in the Organizer. MusicKit supports playlist creation
and additions but does not expose library playlist deletion or removal of selected
playlist songs.

## YouTube Music device-code auth

YouTube Music uses `ytmusicapi` with Google's TV/Limited Input OAuth device
flow. This is the default path when YouTube Music OAuth credentials are set.
The app requests YouTube Data API plus Google userinfo email scope so reconnects
can reuse the same YouTube Music account row by email when Google returns it.

1. Open <https://console.cloud.google.com/apis/library/youtube.googleapis.com>
   and enable the **YouTube Data API v3** for your Google Cloud project.
2. Open <https://console.cloud.google.com/auth/clients>.
3. Create an **OAuth client ID** with application type **TVs and Limited Input
   devices**.
4. Copy the client ID and client secret into the repo-root `.env`:

   ```env
   OPE_YTMUSIC_CLIENT_ID=your_client_id
   OPE_YTMUSIC_CLIENT_SECRET=your_client_secret
   ```

   The device-code prompt should include these scopes:
   `https://www.googleapis.com/auth/youtube` and
   `https://www.googleapis.com/auth/userinfo.email`.

5. Restart the backend and worker:

   ```bash
   docker compose up -d --force-recreate backend worker
   ```

6. In the UI, choose YouTube Music as **From** or **To**, click **Connect YouTube Music**,
   open the verification URL, and enter the displayed code.

YouTube Music **Liked Songs** is backed by the native `LM` playlist. Writing into
it uses YouTube Music's like action rather than creating a normal playlist.

The Organizer verifies `owned=true` with a live playlist read before showing
permanent deletion or song removal. Song removal preserves each `setVideoId`, so
selecting one duplicate occurrence does not remove the others. The pinned
`ytmusicapi` surface has no verified safe playlist-unsubscribe operation, so
followed or ownership-unknown playlists remain unsupported. These destructive
operations require typed confirmation and have no recovery guarantee.

## Liked-track collection mapping

These sources always map to the target provider's native library:

| Source | Target equivalent |
|---|---|
| Tidal **My Collection** | Spotify **Liked Songs** or YouTube Music **Liked Songs** |
| Spotify **Liked Songs** | Tidal **My Collection** or YouTube Music **Liked Songs** |
| YouTube Music **Liked Songs** | Tidal **My Collection** or Spotify **Liked Songs** |

The migration never creates a normal playlist as a fallback for these collections.
If an older OAuth connection lacks a required library scope, preflight asks you to
reconnect before creating the job.

## YouTube Music header-paste fallback

If `OPE_YTMUSIC_CLIENT_ID` and `OPE_YTMUSIC_CLIENT_SECRET` are not set,
self-host mode falls back to header paste. The pasted headers act like a browser
session, so do not share them and clear/sign out of YouTube Music after testing
if they were exposed. Hosted mode does not allow header paste.

If Google blocks device-code auth because the OAuth app is not verified, click
**Use browser-session headers** in the YouTube Music connection panel. The same
guided fallback appears without changing `.env`.

1. Open <https://music.youtube.com> in Chrome or Edge and sign in.
2. Open DevTools with `Cmd+Option+I` on macOS or `Ctrl+Shift+I` on Windows/Linux.
3. Go to **Network**.
4. In YouTube Music, run a search or open a playlist so `music.youtube.com`
   requests appear.
5. Click a `POST` request whose URL starts with one of these:

   ```text
   https://music.youtube.com/youtubei/v1/browse
   https://music.youtube.com/youtubei/v1/music/get_search_suggestions
   https://music.youtube.com/youtubei/v1/search
   ```

   Do not use `jnn-pa.googleapis.com` or other telemetry requests.
6. In **Headers**, copy only the request-header block starting at:

   ```text
   authorization
   ```

   and ending after:

   ```text
   x-youtube-client-version
   <version value>
   ```

   The pasted text must include these entries:

   ```text
   authorization
   ...
   cookie
   ...
   x-goog-authuser
   0
   ```

7. Paste that block into **YouTube Music request headers** in the app and click
   **Connect YouTube Music**.
8. Click **Test connection** before migrating. Header-paste credentials can expire
   with the browser session; reconnect if the test fails. Header-paste sessions do
   not expose the Google account email, so account matching falls back to local
   YouTube Music session identity.

Do not paste response headers (`alt-svc`, `server`, `date`, etc.), pseudo headers
(`:authority`, `:method`, etc.), or the request body.

The app checks the pasted block for `authorization`, `cookie`, `x-goog-authuser`,
and `x-youtube-client-version` before enabling the connect button.

## Safe migration defaults

The app starts deliberately slow for all migrations: 1 playlist per job, 50 tracks
per job, 250 tracks per day, and at least 120 seconds between jobs. If you exceed
those defaults, the UI shows a warning popup and only continues after you
acknowledge it.

Large playlists can take longer than five minutes because each song may require a
YouTube Music search. The worker timeout defaults to 3600 seconds and can be
changed with `OPE_MIGRATION_WORKER_JOB_TIMEOUT_S`.

When a target playlist with the same name already exists, the app reads its songs.
If they overlap with the source, the job reuses that playlist and skips duplicate
songs with a progress notice. If the songs are completely different, the UI warns
before creating a new migrated playlist with the same name.
