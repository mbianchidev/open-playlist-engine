# Scheduled playlist synchronization

Open Playlist Engine can persist a one-way relationship from a source playlist to a
target playlist and run it inside the existing self-hosted worker. Rules, checkpoints,
credentials and execution history stay on the instance.

## Create and manage a rule

1. Complete a migration for one full playlist.
2. Open **Sync** and choose that completed migration.
3. Choose a mode, cadence and IANA timezone, then create the rule.
4. Use **Run now**, **Pause/Resume**, **Save**, or **Delete** from the rule card.
5. Inspect the latest migration when tracks need review or a provider reports a
   partial failure.

Partial-track and multi-playlist migrations cannot become rules because they do not
provide a deterministic baseline. The same endpoint pair can have only one rule, and
an inverse target-to-source rule is rejected to avoid feedback loops.

## Modes

**Add only** matches and writes source tracks first observed after the prior applied
checkpoint. Target-only tracks and target ordering are preserved. Existing target
tracks are reconciled through the migration duplicate logic, so retries do not add
another copy.

**Mirror** matches new source occurrences without appending them, then replaces the
target with the complete mapped URI sequence. It is available only for standard
playlists when the target advertises removal and reordering and implements ordered
replacement. Spotify is the initial mirror target. Liked/saved collections remain
add-only.

Spotify replaces the first 100 items and appends later chunks. A failed multi-batch
replace is marked ambiguous, the worker attempts to restore the prior ordered
sequence, and the retry restarts from the first replacement call.

## Scheduling and restarts

The ARQ worker checks due rules when it starts and every minute. A rule that became
due while the instance was stopped runs once after the worker returns; its next run is
calculated from completion rather than replaying every missed interval.

Postgres stores rules, runs and checkpoints. Valkey stores queued work. Docker Compose
uses `restart: unless-stopped` for the application services, and both backend and
worker apply Alembic migrations before starting.

Only one queued/running execution may exist for a rule. Database uniqueness and row
locks prevent normal overlap. Each active run also owns a lease token; stale recovery
invalidates that lease before scheduling a retry, so an old worker cannot commit a
checkpoint later.

## Failures and review

| State | Behavior |
|---|---|
| `review_required` | Scheduling waits. Resolve the linked migration items; finalization then updates the checkpoint and resumes cadence. |
| `failed` / `partial_failure` | The rule retains its last successful target state and receives a shorter retry time. Already-written additions are reconciled on retry. |
| `reconnect_required` | Expired or missing credentials auto-pause the rule. Reconnect the account, then resume. |
| `paused` | No scheduled run is created. A manual run may still be started. |

Last/next run timestamps, added/removed/reordered counts and the latest error are shown
on each rule.

## Configuration

All values use the `OPE_` prefix:

| Setting | Default | Purpose |
|---|---:|---|
| `SYNC_MIN_CADENCE_MINUTES` | `5` | Smallest accepted interval |
| `SYNC_MAX_CADENCE_MINUTES` | `10080` | Largest accepted interval (one week) |
| `SYNC_RETRY_DELAY_S` | `300` | Default transient-failure retry |
| `SYNC_STALE_RUN_AFTER_S` | `3900` | Active-run recovery threshold; keep above worker timeout |
| `SYNC_SCHEDULER_BATCH_SIZE` | `20` | Due rules claimed per scheduler tick |

The timezone is validated and stored with the rule for schedule presentation. Cadence
is an elapsed interval, so daylight-saving changes do not alter the time between runs.

## API

| Method | Path | Action |
|---|---|---|
| `GET` | `/api/syncs` | List rules with latest run |
| `POST` | `/api/syncs` | Create from a completed migration |
| `GET` | `/api/syncs/{id}` | Get one rule |
| `PATCH` | `/api/syncs/{id}` | Change mode, cadence or timezone |
| `POST` | `/api/syncs/{id}/run` | Queue a manual run |
| `POST` | `/api/syncs/{id}/pause` | Pause |
| `POST` | `/api/syncs/{id}/resume` | Resume |
| `DELETE` | `/api/syncs/{id}` | Delete |
