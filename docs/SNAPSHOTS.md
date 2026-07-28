# Local library snapshots

Open Playlist Engine snapshots are self-hosted, metadata-only backups of the
currently supported universal library entities: standard playlists and native
liked/saved-track collections.

## Storage and persistence

- Local development uses `OPE_SNAPSHOT_DIR=./data/snapshots` by default.
- Docker Compose sets `OPE_SNAPSHOT_DIR=/data/snapshots` and mounts the named
  `snapshots` volume into both `backend` and `worker`.
- No cloud account, object store, or hosted control plane is used.
- The database stores profile/history metadata. Portable `.opb` files live in the
  configured snapshot directory.
- Download important snapshots before deleting the Docker volume or running
  destructive Docker volume cleanup.

The API and worker must point at the same directory. A snapshot written by the
worker cannot be verified, downloaded, imported, or restored by an API container
that uses a different filesystem.

## Creating profiles and snapshots

1. Connect each source provider account.
2. Open **Snapshots** and create a profile.
3. Add one or more accounts and select their playlist or liked-track collections.
4. Set count and age retention.
5. Choose **Create snapshot**.

The worker lists lightweight collection references, then streams each selected
collection with the provider adapter's async item iterator. It does not call the
full in-memory playlist reader. If a provider fails while reading one collection,
captured items remain in the archive, that collection is marked partial, and other
collections continue.

Only one snapshot can be pending or running for a profile. Interrupted jobs become
failed after `OPE_SNAPSHOT_STALE_AFTER_S`; startup reconciliation removes old
temporary/orphan files or restores an interrupted staged deletion.

## Bundle format and integrity

`.opb` files are ZIP64 archives with schema version 1:

```text
manifest.json
manifest.sha256
collections/0001-<collection-uuid>.jsonl
collections/0002-<collection-uuid>.jsonl
...
```

Each collection payload starts with a collection metadata record followed by one
canonical JSON track record per line. The manifest contains:

- snapshot, library-lineage, and source UUIDs;
- creation time and non-secret source labels;
- complete/partial status and failure summaries;
- source, collection, item, and failed-collection counts;
- payload byte counts plus payload and item SHA-256 checksums.

The local database also records the complete archive SHA-256. Verification streams
decompressed data and checks actual byte counts instead of trusting ZIP header
sizes.

Integrity checks detect accidental corruption and unsafe archives. They do not sign
or encrypt a bundle. Anyone who can replace both a bundle and its checksums can
produce a different internally consistent archive.

## Credential and audio exclusion

Bundles never serialize provider credential objects, internal account IDs, access
or refresh tokens, cookies, raw auth headers, private keys, or audio files.
Sensitive URL query parameters are removed. Spotify preview-audio URLs and unknown
opaque provider metadata are omitted. The small v1 metadata allow-list preserves
known non-secret provider identifiers, genres/tags, versions, and popularity.

Imports reject:

- unsafe absolute, parent-directory, nested, or duplicate member paths;
- undeclared files, including audio/binary members;
- unknown or newer schema versions;
- invalid JSON/model records and inconsistent counts;
- credential-like or unsupported metadata fields;
- checksum mismatches, oversized records/archives, excessive expansion, and unsafe
  compression ratios.

`OPE_SNAPSHOT_MAX_MANIFEST_BYTES` and `OPE_SNAPSHOT_MAX_RECORD_BYTES` are separate:
large profiles can have large manifests without allowing an oversized individual
track record.

Verification and failed imports are non-destructive to provider libraries.

## Retention and cleanup

Each profile can set:

- **Keep newest**: maximum usable snapshots retained by count.
- **Maximum age**: delete usable snapshots older than this many days.

A snapshot is eligible when it exceeds either configured limit. Ordering is
deterministic by creation time and snapshot UUID, oldest first for deletion. The
newest usable snapshot is always kept even when it exceeds the age limit. Failed,
pending, and running rows are not treated as retained backups.

Automatic cleanup runs after successful or partial creation. **Clean up now** runs
the same policy manually. Imported profile-less archives are never auto-retained;
delete them explicitly.

Every deletion stages and removes only a generated file inside
`OPE_SNAPSHOT_DIR`. If staging fails, the database row remains.

## History, diff, download, and portability

History shows archive size, schema, item/collection counts, partial status, and
verification state.

- **Verify** rechecks archive, manifest, collection, count, schema, and path safety.
- **Compare** reports added, removed, renamed, and changed collections plus aggregate
  added/removed item counts.
- **Download** returns the portable `.opb` file.
- **Import and verify** requires explicit confirmation, streams the upload with a
  compressed-size limit, rejects duplicate archive hashes, verifies it, and stores
  it under a new local UUID while preserving the original bundle/library lineage.

Downloaded archives can move between self-hosted instances without copying
credentials. Connect a compatible target account on the destination instance before
restoring.

## Restore behavior

Choose **Restore**, select all or some collections, and choose a connected target.
The normal migration endpoints receive `source_snapshot_id`; restore then uses the
existing:

```text
preflight -> match -> review -> write -> report
```

Preflight verifies the archive again, applies target capability checks and safe
limits, warns about partial source collections, and performs same-name target
checks. Low-confidence matches use the existing review UI. Writes use the existing
operation ledger, target playlist reuse, batching, and duplicate detection.

Rerunning the same library lineage is idempotent: previous target playlists are
reused when available, and items already present on the target are skipped instead
of written again. Liked-track collections restore to the target provider's native
liked/saved library when that provider advertises the required capability and
scopes.

## Current limitations

- Snapshots are manual. Recurring schedules depend on the scheduling foundation in
  issue #20.
- The current universal model supports playlists and liked tracks. Saved albums and
  followed artists remain issue #25.
- Archives contain metadata, not audio.
- Provider-specific opaque fields outside the v1 safe allow-list are intentionally
  omitted.
- Integrity uses checksums, not signatures or encryption.
- A partial collection can restore its captured items, but missing provider items
  cannot be reconstructed.
