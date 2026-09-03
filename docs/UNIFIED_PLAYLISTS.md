# Unified playlists

The **Library** workspace provides one provider-neutral view of playlists and songs
from every connected account. It does not move audio or replace provider ownership.
Each provider copy remains native to that service.

## How playlists are grouped

The engine reads playlist metadata and tracks through the existing provider adapters.
It then builds a local projection:

1. Playlists connected by an existing sync rule are always one logical playlist.
2. Other cross-provider copies are grouped when their normalized names match and they
   share at least one identifiable song.
3. Same-name playlists in the same account remain separate unless an explicit sync
   relationship connects them.

Songs merge by the existing migration identity rules: ISRC first, then normalized
title, artist, album, and duration evidence. The track table shows every provider
copy found for each song and marks gaps.

The projection is rebuilt from connected accounts when the Library opens or the user
chooses **Refresh providers**. Provider failures are reported per account or playlist
without hiding data that other providers returned successfully.

## Alignment states

| State | Meaning |
|---|---|
| **Aligned** | Every detected provider copy has the same songs in the same order. |
| **Differences found** | At least one copy is missing songs, has extra songs, or uses a different order. |
| **One provider** | Only one connected copy currently exists. |

## Keep a playlist everywhere

Expand a playlist, choose its **Source of truth**, select a cadence, and choose
**Keep everywhere**.

For every other connected writable account, the engine:

1. runs the normal full-playlist migration preflight;
2. asks the user to acknowledge any safety warnings;
3. creates or reuses the target playlist through the durable migration and review
   pipeline;
4. creates a continuous sync rule only after all failed or review-required tracks are
   resolved.

The selected source remains authoritative. Targets that support ordered replacement
use mirror mode for standard playlists. Other providers and liked-track collections
use add-only mode, which adds new source songs but cannot remove target-only songs or
guarantee ordering.

Setup failures remain visible in the Library. Starting **Keep everywhere** again
retries failed targets without duplicating active or pending syncs.

## API

| Method | Path | Action |
|---|---|---|
| `GET` | `/api/unified-playlists` | Scan connected accounts and return logical playlists, song availability, sync links, and setup state. |
| `GET` | `/api/unified-playlists?refresh=true` | Force provider refresh where the adapter supports cached reads. |

Continuous setup uses the normal `POST /api/migrations` request with a full single
playlist and a `selection.continuous_sync` intent:

```json
{
  "source_provider": "spotify",
  "source_account_id": "source-account-id",
  "target_provider": "tidal",
  "target_account_id": "target-account-id",
  "selection": {
    "playlist_ids": ["playlist-id"],
    "tracks": {},
    "saved_album_ids": [],
    "followed_artist_ids": [],
    "continuous_sync": {
      "mode": "add_only",
      "cadence_minutes": 60,
      "timezone": "Europe/Rome"
    }
  }
}
```

Partial playlist selections, snapshots, imports, albums, and artists cannot request
continuous playlist sync.

## Local-first scope

The unified projection, migrations, credentials, and sync rules stay on the
self-hosted instance. Public SaaS sharing of a logical multi-provider playlist is not
part of this local implementation.
