# Open Playlist Engine

[![CI](https://github.com/mbianchidev/open-playlist-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/mbianchidev/open-playlist-engine/actions/workflows/ci.yml)
[![Documentation](https://github.com/mbianchidev/open-playlist-engine/actions/workflows/pages.yml/badge.svg)](https://mbianchidev.github.io/open-playlist-engine/)

Self-hosted, any-to-any music **playlist and library migration, local-first
generation, and portable export**.

[Website](https://mbianchidev.github.io/open-playlist-engine/) ·
[Overview](#overview) · [Features](#features) ·
[Supported providers](#supported-providers) · [Quick start](#quick-start) ·
[Documentation](#documentation) · [Development](#development)

## Overview

Open Playlist Engine moves music-library metadata between supported providers
without moving or streaming audio. It can:

- migrate playlists, liked tracks, saved albums, and followed or favorite artists;
- import local playlist files, public playlist URLs, and pasted track lists;
- generate editable playlists through an administrator-configured model;
- export, share, snapshot, synchronize, and organize playlists.

It is the first reference implementation of the
[`open-playlist`](https://github.com/mbianchidev/open-playlist) universal
`Playlist`/`Track` specification. Providers are plugin spokes around that universal
format, so each new provider can work with every existing provider as both a source
and a target.

> **Status:** Early MVP. The UI advertises only the capabilities implemented by each
> provider and keeps unsupported operations disabled.

## How it works

```text
provider, file, URL, or text ─▶ [ Open Playlist hub ] ─▶ target provider
                                      │
                                      └───────────────▶ local export
```

Migrations follow a durable **import → match → review → write** pipeline. Matching
uses provider identifiers and an evidence graph; low-confidence results require
human review before anything is written. See
[`docs/DESIGN.md`](docs/DESIGN.md) for the architecture and trust boundaries.

## Features

| Capability | What it provides | Guide |
|---|---|---|
| Playlist migration | Track selection, matching, review, duplicate protection, and live progress | [Design](docs/DESIGN.md) |
| Local file imports | TXT, CSV, M3U/M3U8, PLS, WPL, XSPF, XML, and JSON sources | [Local imports](docs/LOCAL_FILE_IMPORTS.md) |
| URL and text imports | Bounded public playlist URLs and pasted track lists | [Import sources](docs/IMPORT_SOURCES.md) |
| Portable exports | CSV, TXT, M3U8, XSPF, JSON, and multi-playlist ZIP downloads | [Exports](docs/EXPORTING_PLAYLISTS.md) |
| Playlist generator | Private editable drafts resolved to real provider tracks before writing | [Generator](docs/PLAYLIST_GENERATOR.md) |
| Library snapshots | Versioned, metadata-only local backups, verification, diff, and restore | [Snapshots](docs/SNAPSHOTS.md) |
| Scheduled sync | Persistent add-only and capability-gated mirror rules | [Synchronization](docs/SYNCHRONIZATION.md) |
| Playlist Organizer | Capability-gated library cleanup with preflight and durable retries | [Organizer](docs/PLAYLIST_ORGANIZER.md) |
| History and reports | Reopenable migrations, statistics, and streamed CSV/JSON reports | [Migration history](docs/MIGRATION_HISTORY.md) |
| Playlist sharing | Opt-in immutable snapshots with isolated recipient credentials | [Sharing](docs/PLAYLIST_SHARING.md) |

## Supported providers

| Provider | Playlists / liked tracks | Saved albums | Followed/favorite artists |
|---|---|---|---|
| Spotify | Read/write | Read/write | Read/write as follows |
| Tidal | Read/write | Read/write | Read/write as favorites |
| YouTube Music | Read/write | Unsupported | Unsupported |
| Apple Music | Read/write | Unsupported | Unsupported |

Exact source, target, authentication, and Organizer capabilities are provider-driven.
For app registration, callback URLs, scopes, and connection behavior, see
[`docs/CONNECTING_PROVIDERS.md`](docs/CONNECTING_PROVIDERS.md).

## Quick start

Docker Compose starts Postgres, Valkey, the API, worker, and frontend:

```bash
cp .env.example .env
# Edit .env: replace OPE_SECRET_KEY and add credentials for the providers you use.
docker compose build --no-cache
docker compose up
```

- App: <http://localhost:8080>
- API documentation: <http://localhost:8000/docs>
- API health: <http://localhost:8000/health>

All backend settings use the `OPE_` prefix. Start with
[`.env.example`](.env.example), then read the
[configuration guide](docs/CONFIGURATION.md) and
[provider setup guide](docs/CONNECTING_PROVIDERS.md). The optional local Ollama
profile is documented in the
[playlist generator guide](docs/PLAYLIST_GENERATOR.md#docker-compose-ollama-example).

## Development

### Backend

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m alembic upgrade head
uvicorn app.main:app --reload --no-access-log
```

Run the worker in another shell from `backend/`:

```bash
arq app.jobs.worker.WorkerSettings
```

### Frontend

```bash
cd frontend
npm ci
npm run dev
```

The development frontend runs at <http://localhost:5173> and proxies `/api` to the
backend. Required checks and exact local commands are listed in
[`docs/CI.md`](docs/CI.md).

## Repository layout

| Path | Contents |
|---|---|
| [`backend/`](backend/README.md) | FastAPI API, provider adapters, matching, jobs, and database code |
| [`frontend/`](frontend/README.md) | React SPA and generated OpenAPI client |
| [`openapi/`](openapi/open-playlist.yaml) | Vendored Open Playlist specification and generated engine contract; the standard's API reference remains on the [Open Playlist website](https://mbianchidev.github.io/open-playlist/api.html) |
| [`docs/`](docs/README.md) | Architecture, setup, feature guides, and ADRs |

The frontend and backend are hard-separated: the frontend communicates only through
the backend's generated OpenAPI contract.

## Documentation

Browse the rendered [documentation website](https://mbianchidev.github.io/open-playlist-engine/)
or the categorized [`docs/` source index](docs/README.md). Start with:

- [Configuration](docs/CONFIGURATION.md)
- [Connecting providers](docs/CONNECTING_PROVIDERS.md)
- [Architecture and provider contract](docs/DESIGN.md)
- [Continuous integration](docs/CI.md)

## Adding a provider

Implement `ProviderAdapter` in `backend/app/providers/<name>/adapter.py`, declare a
`CapabilityDescriptor`, register it, and pass the conformance suite in
`backend/tests/conformance/`. See the
[provider plugin contract](docs/DESIGN.md#section-5-provider-plugin-contract) and
[`backend/README.md`](backend/README.md).

## License

[MIT](LICENSE)
