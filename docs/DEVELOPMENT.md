# Development

Start with the
[contributor guide](https://github.com/mbianchidev/open-playlist-engine/blob/main/CONTRIBUTING.md).
Coding agents must also read
[AGENTS.md](https://github.com/mbianchidev/open-playlist-engine/blob/main/AGENTS.md).
The [system tour](HOW_IT_WORKS.md), [design](DESIGN.md), and [CI guide](CI.md)
describe the architecture and quality gates.

## Choose a setup

For the complete container stack, follow the
[README quick start](https://github.com/mbianchidev/open-playlist-engine#quick-start).
It supplies the API, worker, frontend, PostgreSQL and Valkey. Rebuild with
`docker compose build --no-cache` after changing source or dependencies.

Native development uses Python 3.12+ and Node.js 26 with npm. CI tests Python 3.12
and Node.js 26; the backend image uses Python 3.14. A modern browser and
PostgreSQL 18/Valkey 9 are required for end-to-end execution.

## Native development

Start from a fork/clone and run the infrastructure steps from the repository root.
The shell examples assume a POSIX shell.

```bash
cp .env.example .env
```

Set a strong `OPE_SECRET_KEY` before connecting accounts and keep `.env` private.
Only configure the providers you use; see [configuration](CONFIGURATION.md) and
[provider setup](CONNECTING_PROVIDERS.md).

### Database and queue

The normal Compose stack does not publish PostgreSQL or Valkey ports. To use its
images with a host-native API, start a separate development project with
loopback-only ports:

```bash
docker compose --project-name ope-native-dev \
  --file docker-compose.yml --file - up --detach --wait postgres valkey <<'YAML'
services:
  postgres:
    ports:
      - "127.0.0.1:5432:5432"
  valkey:
    ports:
      - "127.0.0.1:6379:6379"
YAML
```

These are local development services, not a production exposure pattern. If
those ports are occupied, use separate services/ports and update the connection
URLs below. The project name isolates these resources from the regular stack.

### API

Settings read `.env` from the process working directory. Create a backend-local
copy so Alembic, the API and worker use the same configuration:

```bash
cd backend
cp ../.env .env
```

Set these values in `backend/.env` for the development services:

```dotenv
OPE_DATABASE_URL=postgresql+psycopg://ope:ope@127.0.0.1:5432/ope
OPE_VALKEY_URL=redis://127.0.0.1:6379/0
OPE_FRONTEND_URL=http://localhost:5173
```

Then, from `backend/`:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m alembic upgrade head
uvicorn app.main:app --reload --no-access-log
```

The API is available at `http://localhost:8000`, with interactive documentation at
`/docs` and a basic availability check at `/health`. Provider callbacks still need
the exact registered URLs in the connection guide.

### Worker

In another shell, start from the repository root:

```bash
cd backend
. .venv/bin/activate
arq app.jobs.worker.WorkerSettings
```

Keep the worker and API in the same working directory so they use the same
settings and snapshot path. Scheduled sync, cleanup and durable background work
need the worker, not just a healthy API.

### Frontend

From the repository root in another shell:

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api`, `/health` and `/share` to the
local API. Provider secrets belong in the backend environment, never Vite code.

### Stop and debug

Stop native API, worker and Vite processes with Ctrl+C in their own terminals.
From the repository root, stop the development infrastructure with:

```bash
docker compose --project-name ope-native-dev down
```

Normal shutdown keeps its database volume. Do not use `--volumes` unless you
intend to erase that project's data.

For connection failures, check the API/worker terminal output, active working
directory, URLs and whether Alembic migrations completed. For container
deployments, inspect `docker compose logs --tail=100 backend worker`. Redact
credentials and personal data before sharing diagnostics.

## Checks and synthetic fixtures

Run Ruff, pytest, dependency checks, API/type generation, TypeScript and production
builds using the exact commands in [CI](CI.md). Run focused tests first, then the
checks for every affected surface. There is no configured frontend unit-test or
dedicated accessibility-audit runner; do not substitute a fictitious `npm test`.

Backend provider tests use injected `httpx.MockTransport` fixtures for official
APIs and an in-memory `client_factory` for YouTube Music. Use synthetic data only;
see [ADR 0002](adr/0002-adapter-fixture-testing.md). Do not validate migrations
against real libraries as part of a test run.

For UI changes, verify keyboard operation, focus, narrow layouts and affected
workflows with mocked accounts. Follow
[accessibility verification](https://github.com/mbianchidev/open-playlist-engine/blob/main/ACCESSIBILITY.md#verification).
Regenerate the FastAPI and TypeScript contracts together after public API changes,
as described in the CI guide.

For documentation changes, use a virtual environment from the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --requirement requirements-docs.txt
python -m mkdocs build --strict
```

## Repository layout and extension points

| Path | Contents |
|---|---|
| `backend/` | FastAPI routes, providers, matching, jobs, database and pytest tests |
| `frontend/` | React SPA, product styles and generated API client types |
| `openapi/` | Vendored Open Playlist specification and generated engine contract |
| `docs/` | User/developer guides, architecture, ADRs and site assets |

The frontend and backend are hard-separated. The frontend communicates through
the backend's generated OpenAPI contract.

To add a provider, implement `ProviderAdapter` under
`backend/app/providers/<name>/adapter.py`, declare a `CapabilityDescriptor`,
register it, and pass `backend/tests/conformance/`. Keep matching in core services,
not adapters. See the [provider contract](DESIGN.md#section-5-provider-plugin-contract)
and [backend guide](https://github.com/mbianchidev/open-playlist-engine/blob/main/backend/README.md).

## Build and distribution

The current GitHub releases provide source archives, not a desktop installer or
published application container image. Build the existing Compose images locally
with `docker compose build --no-cache`; the frontend's standalone production
output is `frontend/dist/` after `npm run build`.

The [container CI check](CI.md) builds and smoke-tests the actual stack, including
database upgrades. Documentation builds use `requirements-docs.txt` and MkDocs;
the Pages workflow deploys only from `main` or manual dispatch, not pull requests.

There is no separate release/signing script to invoke. Do not create tags,
GitHub releases, image pushes or signing claims merely to validate documentation
or local builds.
