# Continuous integration

The
[`CI`](https://github.com/mbianchidev/open-playlist-engine/blob/main/.github/workflows/ci.yml)
workflow runs for pull requests, pushes to `main`, and manual dispatches.
Pull-request runs cancel older runs for the same branch.

| Required check | Coverage |
|---|---|
| `Backend` | Python 3.12 editable dev install, `pip check`, Ruff, the full pytest suite, and generated FastAPI OpenAPI drift |
| `Database migrations` | A single Alembic head and `alembic upgrade head` against a clean Postgres 18 database |
| `Frontend` | Node.js 22, deterministic `npm ci`, moderate-or-higher npm advisories, generated TypeScript API drift, explicit type-checking, and the production build |
| `Containers` | PostgreSQL 17-to-18 volume migration, Compose validation, a no-cache image build, full stack startup, and backend plus nginx-proxied health checks |

The jobs use fixture-backed tests and empty provider configuration. No provider
credentials or repository secrets are required.

## Local equivalents

Backend:

```bash
cd backend
python -m venv .venv
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
