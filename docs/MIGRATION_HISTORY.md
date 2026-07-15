# Migration history and reports

The **History** workspace reopens any migration owned by the current user. It extends
the existing statistics view with account context, selected collections, lifecycle
timestamps, duration, acknowledged warnings, final outcome, target links, item-level
results, prior review decisions, and the terminal error when a job failed.

## Inspecting item history

`GET /api/migrations/{job_id}/items` returns ledger rows ordered by playlist and
position. Existing clients can omit paging parameters and continue receiving the
full array. The History UI sends `limit` and `offset`; the response includes the
matching total in `X-Total-Count`.

Supported filters:

| Query parameter | Meaning |
|---|---|
| `source_playlist_id` | Exact source collection ID |
| `status` | Repeatable exact status filter |
| `min_confidence` / `max_confidence` | Inclusive `0.0`–`1.0` match confidence |
| `reason` | Case-insensitive literal substring |
| `title` | Case-insensitive literal substring |
| `artist` | Case-insensitive literal substring |
| `problem_only` | Restrict to `needs_review`, `skipped`, and `failed` |
| `limit` / `offset` | Optional paging; `limit` is capped at 500 |

The endpoint returns `404` when the job is not owned by the current user and `410`
when item detail has expired.

## Downloading reports

`GET /api/migrations/{job_id}/report` generates a report on demand and never stores
an exported file.

- `format=csv|json`
- `scope=all|problems`
- Every item filter above is also accepted.

The UI provides one-click CSV and JSON downloads for all rows and for problem rows.
Both formats stream from a server-side database cursor in configurable batches, so
the complete result is never loaded into application memory.

CSV is UTF-8 with a byte-order mark for spreadsheet compatibility. Every field is
quoted, commas/quotes/newlines use standard CSV escaping, and text beginning with a
spreadsheet formula marker (`=`, `+`, `-`, or `@`) is prefixed with an apostrophe.
JSON is a streamed object with report/job/filter metadata and an `items` array.

## Stable report fields

Report schema version `1` uses the following item fields. JSON preserves
`source_metadata` and `job_warnings` as structured values; CSV serializes them as
compact JSON strings.

| Field | Description |
|---|---|
| `report_version` | Stable report schema version |
| `job_id` | Migration job ID |
| `job_status` | Persisted worker status |
| `job_outcome` | `pending`, `running`, `completed`, `partial`, or `failed` |
| `job_error` | Terminal job error, if any |
| `job_warnings` | Warnings acknowledged before starting |
| `job_created_at` | Job creation timestamp |
| `job_started_at` | Worker start timestamp |
| `job_completed_at` | Terminal timestamp |
| `detail_expires_at` | Item-detail retention deadline |
| `source_provider` | Source provider key |
| `source_account_id` | Historical source account ID |
| `target_provider` | Target provider key |
| `target_account_id` | Historical target account ID |
| `item_id` | Ledger item ID |
| `source_playlist_id` | Source collection ID |
| `source_playlist_name` | Source collection name |
| `target_playlist_id` | Mapped target collection ID |
| `position` | Source position |
| `title` | Source title |
| `artist` | Source artist |
| `album` | Source album |
| `duration_s` | Source duration in seconds |
| `release_year` | Source release year |
| `explicit` | Explicit-content flag |
| `isrc` | Source ISRC |
| `source_track_id` | Provider source track ID from source metadata |
| `source_item_id` | Provider source collection-entry ID |
| `source_uri` | Source provider URI |
| `source_metadata` | Complete universal source-track metadata |
| `target_uri` | Selected/written target URI |
| `target_id` | Provider item ID parsed from the target URI |
| `confidence` | Match confidence from `0.0` to `1.0` |
| `status` | Final item status |
| `reason` | Match, skip, or failure reason |
| `review_action` | Prior `approve` or `skip` action |
| `review_original_status` | Status before the review decision |
| `review_original_reason` | Reason shown before the review decision |
| `reviewed_at` | Review decision timestamp |
| `item_created_at` | Ledger-row creation timestamp |
| `item_updated_at` | Last ledger-row update timestamp |

Reports never include encrypted credentials, refresh/access tokens, raw provider
authentication responses, or another user's jobs.

## Retention

`OPE_MIGRATION_HISTORY_RETENTION_DAYS` defaults to `90`.

- `0` retains item detail indefinitely.
- Job summaries, counts, warnings, lifecycle timestamps, selected collection
  mappings, and terminal errors are retained indefinitely.
- After the deadline, item and report endpoints return `410` immediately.
- The ARQ worker runs bounded cleanup hourly at minute 17. It snapshots the ledger
  summary, then deletes expired `job_item` and `operation_ledger` rows in batches
  controlled by `OPE_MIGRATION_HISTORY_CLEANUP_BATCH_SIZE`.
- Accepted low-confidence review decisions are retained separately so cleanup does
  not make future migrations forget previously approved matches.
- `OPE_MIGRATION_REPORT_BATCH_SIZE` controls the database streaming batch size.

Changing the retention setting applies to new jobs and to older terminal jobs that
do not yet have a persisted deadline.

## API contract

The backend FastAPI contract is checked in at
`openapi/open-playlist-engine.json`. Regenerate it and the frontend TypeScript schema
after changing an API model or route:

```bash
cd backend
.venv/bin/python - <<'PY' > ../openapi/open-playlist-engine.json
import json
from app.main import app
print(json.dumps(app.openapi(), indent=2, sort_keys=True))
PY
cd ../frontend
npm run gen:api
```
