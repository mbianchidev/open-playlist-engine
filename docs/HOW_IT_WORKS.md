# How Open Playlist Engine works

Open Playlist Engine is the concrete, self-hosted implementation of the
[Open Playlist standard](https://mbianchidev.github.io/open-playlist/). The
standard defines provider-neutral music entities. This project supplies the
provider adapters, matching system, durable jobs, safety boundaries, and user
interface needed to use those entities with real libraries.

The engine moves **metadata**, never audio.

## System at a glance

```text
browser
  │
  ▼
React frontend ── /api + server-sent events ──▶ FastAPI service
                                                   │
                         ┌─────────────────────────┼─────────────────────┐
                         ▼                         ▼                     ▼
                   PostgreSQL                  Valkey              provider APIs
                         ▲                         │
                         └──────── arq worker ◀────┘
```

| Component | Responsibility |
|---|---|
| React frontend | Collects selections, renders provider capabilities, presents warnings and review decisions, and follows live progress. |
| FastAPI service | Owns authentication, validation, provider-neutral orchestration, imports, exports, history, and all browser-facing operations. |
| PostgreSQL | Stores encrypted provider credentials, jobs, item ledgers, accepted match evidence, schedules, and snapshot metadata. |
| Valkey | Carries arq jobs and short-lived cache or coordination state. |
| arq worker | Runs provider reads and writes, migration stages, scheduled syncs, snapshots, retention, and organizer operations outside web requests. |
| Provider adapters | Translate one provider's authentication and supported read, search, and write operations into the engine contracts. |

Docker Compose runs these services together. The production frontend uses nginx
to serve the single-page app and proxy same-origin `/api` requests to FastAPI.

## The universal hub

The core model represents playlists, tracks, albums, and artists without making
one provider's identifiers or behavior universal. Provider-specific identifiers
remain evidence attached to those entities.

Each provider is a **spoke**:

1. An adapter declares exactly which source, target, authentication, and
   maintenance capabilities it supports.
2. It converts provider responses into the universal model and performs only its
   own provider reads, searches, and writes.
3. Core services own matching, review policy, duplicate handling, jobs, and
   persistence.

This keeps the integration cost proportional to the number of providers rather
than the number of source/target pairs. It also lets the frontend disable an
unsupported operation before a user starts it.

## A migration from request to result

### 1. Import and select { #migration-import }

A source can be a connected provider, a bounded local playlist file, a supported
public playlist URL, pasted text, a local snapshot, or an approved generator
draft. The API normalizes the source into universal entities and returns only the
collections and actions that the source adapter supports.

Local files, URLs, and pasted text use expiring, owner-scoped import snapshots.
Once queued, the worker reads the exact normalized snapshot that the user
reviewed; later source changes cannot alter that job.

### 2. Preflight { #migration-preflight }

The API resolves account ownership and target capabilities again on the server.
It counts selected entities, detects risky same-name targets, and returns explicit
warnings for operations outside conservative defaults. The browser can proceed
only after the user acknowledges those warnings.

### 3. Match { #migration-match }

The worker searches the target through its adapter. `MatchService` combines
strong identifiers such as provider IDs and ISRCs with normalized metadata and
previously accepted evidence. Matching policy is centralized, so adapters cannot
quietly lower the confidence threshold.

### 4. Review { #migration-review }

High-confidence items can continue automatically. Low-confidence or ambiguous
items become `needs_review`. A user can accept the suggestion, supply a supported
replacement identifier, or skip the item. Accepted decisions enrich the private
evidence graph for later work.

### 5. Write and report { #migration-write }

Confirmed items are written through the target adapter. A persisted operation
ledger makes retries idempotent, and duplicate checks avoid repeating work on
partial reruns. The browser receives item-level status through server-sent events;
the same rows later power history, statistics, and downloadable reports.

## Workflows that reuse the same safety model

The migration ledger is the common execution path rather than a one-off transfer
screen:

- **Portable exports** branch from the universal model before target matching and
  serialize selected playlists without requiring a destination provider.
- **Generator drafts** remain private and editable until every suggestion resolves
  to a real target item and the user confirms the write.
- **Snapshot restores** change the source reader but retain target preflight,
  matching, review, idempotency, and progress.
- **Scheduled synchronization** starts from a completed migration, persists
  checkpoints, and creates normal reviewable migration work for changes.
- **Playlist sharing** publishes immutable metadata snapshots while isolating a
  recipient's credentials and jobs from the owner's accounts.
- **Playlist Organizer** uses a separate durable job type, server-side capability
  checks, explicit destructive confirmations, and failed-item-only retries.

## Trust and data boundaries

- Provider tokens are encrypted at rest and never returned to the frontend.
- Browser input does not choose an arbitrary adapter, filesystem path, or network
  destination.
- Public URL imports accept registered provider URL shapes and apply bounded,
  SSRF-safe fetching; arbitrary web pages are not scraped.
- File imports stream into bounded parsers and persist normalized metadata rather
  than a user-supplied server path.
- Spreadsheet exports neutralize formula prefixes, and XML-based formats remove
  illegal controls.
- Snapshots contain metadata, not provider credentials or audio.
- Generated playlists require resolved provider tracks and an explicit
  confirmation before any write.

See the [design reference](DESIGN.md#section-12-security-privacy) for the complete threat
model and implementation constraints.

## Frontend and backend stay separate

The frontend contains presentation and browser interaction only. FastAPI remains
the source of truth for business rules and publishes the contract used to
generate the checked-in TypeScript API types. This hard boundary prevents the UI
from inventing provider behavior and keeps validation authoritative on the
server.

For development, CI independently checks backend lint and tests, database
migrations, generated-contract drift, frontend types and build output, and the
complete container stack. See [Continuous integration](CI.md).

## Adding another provider

A provider integration:

1. implements `ProviderAdapter` and only the optional capability contracts the
   provider can honor;
2. declares its `CapabilityDescriptor` and registers itself;
3. uses an injected network or client seam for deterministic fixtures; and
4. passes the shared conformance suite.

No source-to-target conversion matrix is added. Once registered, the provider
uses the same model, matching, review, job, and history services as every other
spoke.

Continue with the detailed
[provider plugin contract](DESIGN.md#section-5-provider-plugin-contract) or the
[provider connection guide](CONNECTING_PROVIDERS.md).
