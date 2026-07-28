# Playlist Organizer

Playlist Organizer is the maintenance workspace for connected provider libraries.
It keeps non-destructive removal separate from permanent deletion, requires a
preflight before every job, and records one durable result per playlist.

## Safety model

- **Remove from library** is the default. It only appears when the provider exposes
  a verified non-destructive unfollow/library-removal operation.
- **Delete permanently** is a separate mode. It only applies to playlists whose
  ownership is confirmed during preflight and requires an exact typed phrase.
- **Remove songs** targets explicit playlist entries. It never removes duplicate
  candidates automatically and also requires typed confirmation.
- Unsupported actions stay visible as provider limitations; the backend rejects
  unsupported selections even if a client bypasses the UI.
- Liked/saved-track collections are not treated as ordinary playlists by the
  organizer.

## Provider support

| Provider | Safe playlist removal | Permanent playlist deletion | Remove selected songs | Recovery implications |
|---|---|---|---|---|
| Spotify | Yes. Uses the generic library removal endpoint to unfollow/remove the playlist from the current user's library. | No. Spotify does not expose destructive playlist deletion. | Yes for owned or collaborative playlists. Uses URI + exact positions + `snapshot_id`; maximum 100 selected entries per job. | Removed playlists can be followed again while still available. Song removals must be re-added manually. |
| YouTube Music | No verified safe playlist-unsubscribe operation in the pinned `ytmusicapi` surface. | Yes, only when a live playlist read reports `owned=true`. | Yes for owned playlists using the per-occurrence `setVideoId`. | Deletion and item removal are unofficial API operations with no recovery guarantee. |
| Tidal | No verified safe unfollow operation in the current adapter. | Yes for playlists returned by Tidal's owner-filtered listing. | No. Exact duplicate-occurrence removal is not exposed until the public behavior can be verified. | Deletion is permanent from Open Playlist Engine's perspective. |
| Apple Music | No. | No. | No. | MusicKit currently exposes create/add operations but not playlist deletion or item removal. |

## Workflow

1. Open **Organizer** and choose a connected library.
2. Search, sort, or filter by ownership. Provider dates are shown when available.
3. Choose **Remove from library**, **Delete permanently**, or **Remove songs**.
4. Select playlists or expand a playlist and select exact song entries.
5. Review the preflight receipt. It groups the exact provider operation, ownership,
   collaboration state, unsupported selections, and recovery impact.
6. Type the displayed phrase for deletion or song removal.
7. Follow the operation ledger. Partial failures remain visible per playlist and
   **Retry failed only** never reruns successful items.

## Durable jobs and retries

`organizer_job` stores the account-level request and aggregate status.
`organizer_item` stores one playlist/action result, including immutable provider
identifiers, attempt count, retryability, and per-song results.

- Successful items are excluded from later worker runs.
- Safe removal and playlist deletion treat an already-absent playlist as complete.
- Spotify song removal stores baseline and expected sequence hashes. After an
  ambiguous response, the worker only marks success when the exact expected playlist
  sequence is observed; any other snapshot change requires a new preflight.
- YouTube Music retries compare `setVideoId` values and send only entries that still
  exist.
- Provider/account writes pass through the central token bucket. Bounded automatic
  retries apply to rate-limit responses; reconnectable auth failures remain
  retryable from the UI.
- Playlist caches are invalidated after any successful organizer work, then the UI
  refreshes without hiding failed items.

The migration `operation_ledger` is intentionally not reused: it has migration-job
foreign keys, while organizer idempotency lives directly on `organizer_item`.

## Duplicate review

Duplicate scanning is read-only. Candidates require:

- the same normalized playlist name;
- compatible owner identity when the provider exposes it; and
- at least 50% track overlap against the smaller playlist.

The result explains the evidence and offers a focused review view. It never selects,
unfollows, deletes, or edits either playlist.

## API

- `GET /api/organizer/playlists`
- `POST /api/organizer/preflight`
- `POST /api/organizer/duplicates`
- `GET|POST /api/organizer/jobs`
- `GET /api/organizer/jobs/{job_id}`
- `POST /api/organizer/jobs/{job_id}/retry`

All routes use the server-resolved current user. Hosted mode remains closed until
real user authentication is configured.
