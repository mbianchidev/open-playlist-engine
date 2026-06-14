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

## YouTube Music header-paste auth

YouTube Music uses an unofficial self-host-only auth path. The pasted headers act
like a browser session, so do not share them and clear/sign out of YouTube Music
after testing if they were exposed.

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

Do not paste response headers (`alt-svc`, `server`, `date`, etc.), pseudo headers
(`:authority`, `:method`, etc.), or the request body.
