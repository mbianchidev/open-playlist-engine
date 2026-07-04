# Connecting Spotify and YouTube Music

This self-hosted MVP migrates from Spotify to YouTube Music. Keep `.env` local:
it contains secrets and session tokens.

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

10. In the UI, choose Spotify as **From**, click **Connect Spotify**, and approve
    the requested scopes.
11. Use **Test connection** after connecting. Spotify refresh tokens expire after
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

6. In the UI, choose YouTube Music as **To**, click **Connect YouTube Music**,
   open the verification URL, and enter the displayed code.

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

The app starts deliberately slow for Spotify → YouTube Music migrations: 1
playlist per job, 50 tracks per job, 250 tracks per day, and at least 120 seconds
between jobs. If you exceed those defaults, the UI shows a warning popup and only
continues after you acknowledge it.

Large playlists can take longer than five minutes because each song may require a
YouTube Music search. The worker timeout defaults to 3600 seconds and can be
changed with `OPE_MIGRATION_WORKER_JOB_TIMEOUT_S`.

When a target playlist with the same name already exists, the app reads its songs.
If they overlap with the source, the job reuses that playlist and skips duplicate
songs with a progress notice. If the songs are completely different, the UI warns
before creating a new migrated playlist with the same name.
