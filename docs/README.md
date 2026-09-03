# Documentation

Read these guides on the
[Open Playlist Engine documentation website](https://mbianchidev.github.io/open-playlist-engine/)
or browse their Markdown sources below.

Use this index to find setup, architecture, and feature-specific guides. For the
project overview and quick start, return to the [root README](../README.md).

## Getting started

- [Product purpose and design principles](PRODUCT.md) — audience, goals, brand
  personality, interface principles, and accessibility targets.
- [Configuration](CONFIGURATION.md) — environment variables, deployment modes, and
  operational relationships.
- [Connecting providers](CONNECTING_PROVIDERS.md) — Spotify, Tidal, Apple Music, and
  YouTube Music setup.
- [Continuous integration](CI.md) — required checks and their local equivalents.

## Architecture

- [How Open Playlist Engine works](HOW_IT_WORKS.md) — concise system tour from
  browser request through matching, review, and durable provider writes.
- [Design](DESIGN.md) — product flow, hub-and-spoke architecture, provider contract,
  data model, and security boundaries.
- [ADR 0001](adr/0001-architecture-decisions.md) — initial architecture decisions.
- [ADR 0002](adr/0002-adapter-fixture-testing.md) — provider adapter test seams and
  fixtures.

## Import and export

- [Local playlist-file imports](LOCAL_FILE_IMPORTS.md) — supported formats, parsing,
  limits, and API usage.
- [Public URL and pasted-text imports](IMPORT_SOURCES.md) — accepted URLs and rows,
  limits, and network safety.
- [Exporting playlists](EXPORTING_PLAYLISTS.md) — portable formats, archives,
  warnings, and API usage.

## Library workflows

- [Unified playlists](UNIFIED_PLAYLISTS.md) — cross-provider playlist identity, song
  coverage, alignment, and keep-everywhere synchronization.
- [Playlist generator](PLAYLIST_GENERATOR.md) — local model setup, controls, privacy,
  review, and confirmation.
- [Local library snapshots](SNAPSHOTS.md) — storage, verification, retention,
  portability, and restore.
- [Scheduled synchronization](SYNCHRONIZATION.md) — recurring rules, modes,
  scheduling, and recovery.
- [Playlist Organizer](PLAYLIST_ORGANIZER.md) — safety model, provider support, jobs,
  and retries.
- [Migration history](MIGRATION_HISTORY.md) — item history, reports, fields, and
  retention.
- [Playlist sharing](PLAYLIST_SHARING.md) — public setup, security, downloads, and
  recipient imports.

## Component guides

- [Backend](../backend/README.md)
- [Frontend](../frontend/README.md)
