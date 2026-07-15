# Self-hosted playlist generator

The playlist generator turns a prompt and explicit music controls into a private,
editable draft. The model never writes to a streaming service. Open Playlist Engine
resolves model suggestions against a connected target provider, and the user must
review and confirm the final list before the durable migration worker creates anything.

## Local OpenAI-compatible setup

Generation is disabled by default. Configure both an endpoint and model:

```dotenv
OPE_GENERATOR_BACKEND=openai_compatible
OPE_GENERATOR_OPENAI_BASE_URL=http://localhost:11434/v1
OPE_GENERATOR_MODEL=qwen3:8b
OPE_GENERATOR_OPENAI_API_KEY=
```

The endpoint must implement `POST /chat/completions` with OpenAI-compatible messages,
`response_format={"type":"json_object"}`, and `max_tokens`.

### Docker Compose Ollama example

The optional `generator` profile runs Ollama without making it a required dependency:

```bash
docker compose --profile generator up -d ollama
docker compose exec ollama ollama pull qwen3:8b
```

When the backend also runs in Compose, use the service hostname:

```dotenv
OPE_GENERATOR_OPENAI_BASE_URL=http://ollama:11434/v1
OPE_GENERATOR_MODEL=qwen3:8b
```

Restart the backend after editing `.env`:

```bash
docker compose up -d --force-recreate backend worker
```

For an endpoint running directly on the host, use `http://host.docker.internal:<port>/v1`
from Docker Desktop. Linux installations may need an explicit host-gateway mapping.

## Optional GitHub Copilot SDK

The backend includes `github-copilot-sdk`. Select it explicitly:

```dotenv
OPE_GENERATOR_BACKEND=copilot_sdk
OPE_GENERATOR_MODEL=auto
OPE_GENERATOR_COPILOT_GITHUB_TOKEN=
```

The SDK can use an authenticated Copilot CLI environment. In a container or unattended
deployment, set `OPE_GENERATOR_COPILOT_GITHUB_TOKEN` to an administrator-provided token
that is authorized for GitHub Copilot.

Copilot SDK mode is optional and is not local-only: the bounded generation context is
sent to the selected GitHub Copilot model. The default OpenAI-compatible mode does not
require a hosted AI account.

## Controls

The Generator workspace supports:

- Natural-language prompt.
- Genres, moods, eras or decades.
- Energy, duration, and track count.
- Seed artists and tracks.
- Explicit-content preference.
- Familiarity and discovery levels.
- Connected target provider account.

Hard request limits are 2,000 prompt characters and 50 tracks. The administrator can
lower the active track limit (25 by default), model output size, output tokens, and
timeout:

```dotenv
OPE_GENERATOR_TIMEOUT_S=60
OPE_GENERATOR_MAX_PROMPT_CHARS=2000
OPE_GENERATOR_MAX_OUTPUT_CHARS=32000
OPE_GENERATOR_MAX_OUTPUT_TOKENS=4096
OPE_GENERATOR_MAX_TRACKS=25
```

## Privacy boundaries

| Data | Model receives it? | Persisted? | Logged? |
|---|---:|---:|---:|
| User prompt | Yes, only when Generate is pressed | No | No |
| Explicit controls | Yes | Only resolved draft state | No |
| Provider credentials or account IDs | No | Credentials remain encrypted in existing tables | No |
| Raw provider responses | No | No | No |
| Full listening or migration history | No | Existing local cache/job data remains local | No |
| Opt-in preference summary | Yes, capped top artists/genres and count only | Yes, private per user | No |
| Resolved draft tracks | No additional model call | Yes, private until deleted/confirmed | Operational IDs only |

Copilot SDK sessions use empty mode with no tools, file context, memory, skills,
configuration discovery, host Git operations, session store, or session telemetry.

## Personalization

Personalization is off by default. Enabling it derives a bounded aggregate from up to
500 locally cached playlist or migration items. Only top artists, top genres, and the
source item count are stored and sent. Track titles and raw history are not included.

Turning personalization off stops using the stored summary. **Delete data** removes the
summary entirely. Enabling it again rebuilds the summary from the current local cache.

## Resolution and review

The model output is strict JSON containing a playlist name, optional description, and
title/artist search intents. Unknown fields, malformed JSON, oversized output, and more
tracks than requested are rejected.

Every suggestion is searched through the selected provider adapter and scored by the
existing `MatchService`:

- High-confidence unique candidates are marked **Resolved**.
- Low-confidence or live-version candidates are marked **Review** and require an
  explicit approval or replacement.
- Missing candidates are marked **Unresolved** and must be removed or replaced.
- Duplicate provider URIs are removed.

The review workspace can rename the playlist, edit its description, reorder, remove,
approve, search replacements, add provider tracks, or regenerate. Added and replacement
tracks are revalidated against provider search results.

## Confirmation and durable writes

The generation request and all draft edits are non-writing operations. Final
confirmation:

1. Rejects empty, unresolved, unreviewed, duplicate, or no-longer-valid provider URIs.
2. Runs target capability and conservative migration preflight checks.
3. Requires a second acknowledgement if safe track/day/spacing or same-name warnings
   apply.
4. Snapshots approved URIs into standard `MigrationJob` and `JobItem` rows.
5. Enqueues the existing worker.

The worker skips model calls and matching. It writes only the reviewed URIs, using the
existing playlist reuse, duplicate checks, provider batch limits, operation ledger,
progress events, and statistics.

Generated drafts expose a universal `Playlist` snapshot in the API, so future portable
export and self-hosted sharing features can consume the same provider-agnostic shape
without exposing internal account IDs or prompts.

## Failure behavior and limitations

- Missing configuration returns a setup message and HTTP 503.
- Unreachable model endpoints return HTTP 503; model timeouts return HTTP 504.
- Invalid structured output returns HTTP 502 without echoing the model response.
- Generation and provider resolution happen in one request. Larger local models or
  rate-limited providers can take time; lower `OPE_GENERATOR_MAX_TRACKS` when necessary.
- A model can suggest a nonexistent or incorrect song. It is never treated as a
  successful item unless the target provider returns a real candidate.
- OpenAI-compatible servers differ in JSON-mode support. Use an endpoint/model that
  honors `response_format` and produces the documented schema.
