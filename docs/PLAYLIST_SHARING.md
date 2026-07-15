# Self-hosted playlist sharing

Open Playlist Engine can publish an immutable, metadata-only playlist snapshot
from a self-hosted instance. A recipient can view the snapshot, download a
portable file, or connect their own provider account and import through the
existing match, review, and write pipeline.

Public sharing is disabled by default.

## Configure the instance

Generate separate high-entropy values for encryption and owner access:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Set the first output as `OPE_SECRET_KEY` and the second as
`OPE_OWNER_ACCESS_TOKEN`, then configure the public origin:

```dotenv
OPE_PUBLIC_BASE_URL=https://music.example.com
OPE_FRONTEND_URL=https://music.example.com
OPE_SECRET_KEY=<first generated value>
OPE_OWNER_ACCESS_TOKEN=<second generated value>
```

`OPE_PUBLIC_BASE_URL` must be empty to keep sharing off. Once it is set, every
private self-host API requires the signed owner session created by entering
`OPE_OWNER_ACCESS_TOKEN` in the UI. The access token is never stored in browser
storage; the backend issues a Secure, HttpOnly, SameSite cookie.

For recipient Spotify or Tidal OAuth, register and configure callback URLs on the
same public origin:

```dotenv
OPE_SPOTIFY_REDIRECT_URI=https://music.example.com/api/auth/spotify/callback
OPE_TIDAL_REDIRECT_URI=https://music.example.com/api/auth/tidal/callback
```

The public recipient endpoint rejects redirect OAuth when its configured callback
origin does not match `OPE_PUBLIC_BASE_URL`. YouTube Music device-code/header
flows and Apple Music MusicKit use their existing configuration.

## Reverse proxy and logs

The bundled nginx configuration:

- proxies `/share/` to the backend for escaped Open Graph metadata;
- serves `/shared/` from the React SPA;
- proxies `/api/public/shares/` for snapshots, downloads, recipient auth, imports,
  progress, and review;
- applies a per-client request limit to these public routes;
- disables access/error logs on token-bearing paths; and
- sends `Referrer-Policy: no-referrer`.

The backend additionally applies a per-share/action token bucket and redacts share
tokens from application logs. Docker starts uvicorn with `--no-access-log`, and
the direct backend port binds to `127.0.0.1`. Keep port 8000 private when using a
different reverse proxy, suppress or redact the three token-bearing paths above,
and preserve the no-referrer policy.

The current self-host OAuth adapters keep pending provider state in one backend
process. Run one backend replica/worker for recipient redirect OAuth. The arq job
worker remains a separate process.

## Publish and manage a snapshot

1. Connect the source account in **Migration**.
2. Open **Sharing**.
3. Choose the source account and playlist.
4. Add optional attribution.
5. Choose **Unlisted** or **Public** and an optional expiration.
6. Select **Publish immutable snapshot**, then copy or open the URL.

Public and unlisted links both require a 256-bit URL token. Unlisted pages send
`noindex,nofollow`; public pages permit indexing. There is no token-free public
directory.

The snapshot contains only an explicit allowlist of playlist and track metadata:
title, description, approved cover/artwork URLs, attribution, source links,
ordering, artist, album, duration, release year, explicit flag, ISRC, media type,
and unsupported reason. It excludes credentials, raw provider responses,
provider account IDs, owner IDs, provider snapshot IDs, jobs, and arbitrary
metadata.

Publishing copies the metadata once. Later edits at the source do not mutate the
link. Publish a new share to capture a new source state.

Owners can:

- inspect the exact stored track list;
- copy or open the link;
- switch between public and unlisted;
- expire the link immediately; or
- revoke it permanently.

Expired and revoked pages return HTTP 410 and cannot start new downloads, auth
flows, or imports.

## Recipient downloads and imports

Recipients can download:

| Format | Use |
|---|---|
| Open Playlist JSON | Lossless represented metadata and round trips |
| CSV | Spreadsheet tools |
| TXT | Simple human-readable track list |
| M3U8 | Playlist-capable players when source URLs are available |
| XSPF | Open XML playlist tools |

Downloads are bounded by `OPE_SHARE_MAX_DOWNLOAD_BYTES`. CSV values are protected
against spreadsheet formula injection; text/M3U fields are single-line; XML is
escaped.

Import requires a signed recipient session and a provider account connected from
that share page. Accounts are keyed to the share recipient, never `local`, so
owner accounts cannot be listed, selected, or written. Redirect OAuth identity is
resolved from a hashed, expiring state record rather than caller input.

Recipient credentials remain encrypted only for
`OPE_SHARE_RECIPIENT_CREDENTIAL_RETENTION_S` (24 hours by default) and are removed
opportunistically after expiry. Revoking a share blocks new work immediately but
does not strand a migration already created from its copied snapshot; an open
recipient page can finish progress and review.

Public imports have hard per-share concurrent and daily track caps in addition to
the normal migration warnings. Relevant settings are documented in
[`.env.example`](../.env.example).

## Limits and artwork

`OPE_SHARE_MAX_TRACKS` and `OPE_SHARE_MAX_SNAPSHOT_BYTES` cap publication and are
rechecked on public reads. Artwork is loaded directly only from HTTPS hosts in
`OPE_SHARE_ARTWORK_HOSTS`; other URLs are omitted. Add a host only when its URLs
are safe to expose directly to recipient browsers.
