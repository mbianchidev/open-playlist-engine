# Continuous integration

The
[`CI`](https://github.com/mbianchidev/open-playlist-engine/blob/main/.github/workflows/ci.yml)
workflow runs for pull requests, pushes to `main`, and manual dispatches.
Pull-request runs cancel older runs for the same branch.

| Required check | Coverage |
|---|---|
| `Backend` | Python 3.15 editable dev install, `pip check`, Ruff, the full pytest suite, and generated FastAPI OpenAPI drift |
| `Database migrations` | Python 3.15, a single Alembic head and `alembic upgrade head` against a clean Postgres 18 database |
| `Frontend` | Node.js 26, deterministic `npm ci`, moderate-or-higher npm advisories, generated TypeScript API drift, and the production build including TypeScript checking |
| `Containers` | PostgreSQL 17-to-18 volume migration, Compose validation, a no-cache image build, full stack startup, and backend plus nginx-proxied health checks |

The jobs use fixture-backed tests and empty provider configuration. No provider
credentials or repository secrets are required. The backend/worker image also
uses Python 3.15; the package minimum and Ruff syntax target remain Python 3.12.

The Tidal rate-limit test records and checks retry delays instead of waiting in
real time; production retry timing is unchanged. The frontend's `npm run build`
already runs `tsc --noEmit`, so CI does not run the same type-check twice.

After database migrations, Compose replaces the API and worker startup shells
with their application processes using `exec`. Shutdown signals reach those
processes directly, allowing graceful cleanup without waiting for the shell's
stop timeout. The container check still builds without cache and retains its
full smoke test.

## Documentation and Pages

The
[`Documentation`](https://github.com/mbianchidev/open-playlist-engine/blob/main/.github/workflows/pages.yml)
workflow validates changes to `docs/`, `mkdocs.yml`, `requirements-docs.txt`, and
its own workflow file. It installs the pinned documentation requirements on
Python 3.15 and runs a clean `python -m mkdocs build --strict`, then checks that
neither OpenAPI contract is included in the site. API generation and drift checks
remain in `CI`; the documentation build does not generate or publish those contracts.

The build uses a dedicated pip download cache at `.cache/pip-docs` in the build
workspace so unrelated preloaded packages are not restored and saved with the
documentation dependencies. The existing `setup-python` cache still keys on
the runner platform, Python version, and `requirements-docs.txt`. Both cache hits
and misses run the same dependency installation and strict build; installed
environments and built sites are not cached.

Pull requests only build and validate, and newer runs cancel obsolete builds
for the same PR. Pushes to `main` and manual dispatches upload the checked site
as the run's `github-pages` artifact for the dependent deployment job. The
existing `github-pages` environment restricts deployment to `main`. Running
push/manual workflows finish before a newer run for the same ref starts; only
the newest pending run is retained.

For recovery, re-run a known-good deployment job while its artifact is available
(the Pages artifact retention is one day), or manually dispatch the workflow on
`main` to rebuild and deploy its current documentation. Do not dispatch a
deployment just to measure build performance.

## Local equivalents

Backend:

```bash
cd backend
python3.15 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
python -m ruff check .
python -m pytest
```

Regenerate and check the API contract after backend route or model changes:

```bash
cd backend
python - <<'PY'
import json
from pathlib import Path

from app.main import app

Path("../openapi/open-playlist-engine.json").write_text(
    json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
git diff --exit-code -- ../openapi/open-playlist-engine.json

cd ../frontend
npm ci --no-audit --no-fund
npm audit --audit-level=moderate
npm run gen:api
git diff --exit-code -- src/api/schema.d.ts
npm run typecheck
npm run build
```

The container check exercises the PostgreSQL 17-to-18 dump/restore path before
starting the application on PostgreSQL 18:

Future Postgres major versions remain manual upgrades because storage formats and
volume layouts can change. Dependabot keeps Compose on the supported major while
still proposing minor and patch updates.

```bash
cp .env.example .env
docker compose config --quiet
docker compose build --no-cache
docker compose up --detach postgres valkey backend
curl --fail http://127.0.0.1:8000/health
docker compose up --detach worker frontend
curl --fail http://127.0.0.1:8080/health
docker compose down --volumes
```
