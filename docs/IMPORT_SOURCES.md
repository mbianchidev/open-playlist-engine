# Public URL and pasted-text imports

Open Playlist Engine can normalize a public playlist URL or pasted track list into
the same private preview used by local playlist-file imports. After preview and
selection, tracks enter the standard match, review, progress, and write pipeline.

## Workflow

1. Choose **Public playlist URL** or **Pasted track list** as the source.
2. Enter the URL or text and create a preview.
3. Review normalized tracks and line-level warnings.
4. Select tracks, connect a supported target provider, and start the migration.

The preview is owner-scoped and lease-backed. It captures the normalized source
before the job is queued, so retries or later changes to the remote playlist cannot
silently change the selected input.

## Supported public URLs

| Source | Accepted shape | Source access |
|---|---|---|
| Spotify | `https://open.spotify.com/playlist/{id}` and locale-prefixed `/intl-xx/playlist/{id}` | Requires a connected Spotify source account; Spotify may restrict playlists the account does not own or collaborate on |
| YouTube Music | `https://music.youtube.com/playlist?list={id}` and equivalent `youtube.com` playlist URLs | Public playlists use an unauthenticated reader; private or unavailable playlists request a YouTube Music connection |
| Apple Music | `https://music.apple.com/{storefront}/playlist/{slug}/{id}` | Uses the configured MusicKit developer token; no Music User Token is required for public catalog playlists |
| Tidal | `https://tidal.com/browse/playlist/{uuid}`, `https://listen.tidal.com/browse/playlist/{uuid}`, or `/playlist/{uuid}` | Requires a connected Tidal source account |
| Open Playlist Engine | `https://{allowed-host}/share/{token}` | The host must be in `OPE_IMPORT_OPEN_PLAYLIST_HOSTS` or match the HTTPS `OPE_PUBLIC_BASE_URL` host |

The application does not scrape arbitrary web pages. URLs must match an exact
provider host and path recognized by the import resolver.

## Pasted text

Blank lines and lines beginning with `#` are ignored. Unicode and duplicate tracks
are preserved. Common row forms include:

```text
Artist - Title
Artist<TAB>Title<TAB>Album<TAB>ISRC
artist<TAB>title<TAB>album<TAB>isrc
Title without an artist
```

Headered tabular input can use tabs, commas, semicolons, or pipes. Missing titles and
overlong rows are reported as line-level errors and skipped; title-only rows remain
selectable with a missing-artist warning.

## Limits

| Setting | Default | Purpose |
|---|---:|---|
| `OPE_IMPORT_MAX_TEXT_BYTES` | `262144` | Maximum pasted-text input |
| `OPE_IMPORT_MAX_ITEMS` | `1000` | Maximum normalized tracks |
| `OPE_IMPORT_MAX_LINE_CHARS` | `2000` | Maximum characters per input row |
| `OPE_IMPORT_MAX_FIELD_CHARS` | `500` | Maximum characters per parsed field |
| `OPE_IMPORT_MAX_URL_CHARS` | `2048` | Maximum source URL length |
| `OPE_IMPORT_MAX_RESPONSE_BYTES` | `2000000` | Maximum remote response |
| `OPE_IMPORT_MAX_REDIRECTS` | `3` | Maximum remote redirects |
| `OPE_IMPORT_HTTP_TIMEOUT_S` | `10` | Remote request timeout |
| `OPE_IMPORT_OPEN_PLAYLIST_HOSTS` | Empty | Additional HTTPS hosts allowed to serve Open Playlist Engine shares |

Change these values in `.env`; see the
[configuration guide](CONFIGURATION.md).

## Network safety

Open Playlist Engine share imports reject URL credentials, non-default ports,
IP-literal hosts, localhost, private, link-local, or reserved DNS answers, redirects
outside the allowlist, compressed bodies, excessive redirects, and oversized
responses. DNS is validated immediately before connecting, and the HTTPS socket is
pinned to the validated public address.

Provider URLs are parsed locally against strict HTTPS host and path allowlists.
Spotify and Tidal access remains bound to the connected account rather than
bypassing provider controls.

For binary playlist uploads and their retention behavior, see
[Local playlist-file imports](LOCAL_FILE_IMPORTS.md).
