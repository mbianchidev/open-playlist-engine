# Repository guidance for coding agents

Read [CONTRIBUTING.md](CONTRIBUTING.md), the
[development guide](docs/DEVELOPMENT.md), and the [CI guide](docs/CI.md) before
changing code. More-specific nested instructions take precedence for their paths.

## Repository map

- `backend/app/api/`: FastAPI routes and request/response models.
- `backend/app/core/`: universal models, capabilities, matching, preflight and
  provider-neutral behavior.
- `backend/app/providers/`: bundled provider adapters; see the
  [provider contract](docs/DESIGN.md).
- `backend/app/jobs/`, `imports/`, `exports/`, `snapshots/`: durable workflows.
- `backend/migrations/`: Alembic migrations.
- `backend/tests/`: pytest coverage and synthetic provider fixtures.
- `frontend/src/`: React/TypeScript app. `index.css` provides base styles;
  `theme.css` owns product tokens and responsive presentation.
- `openapi/open-playlist-engine.json` and `frontend/src/api/schema.d.ts`: generated
  API contract and types. Do not hand-edit them.
- `docs/`: user and developer documentation, built with MkDocs.

## Setup and quality gates

Use Python 3.15 and Node.js 26. The backend minimum remains Python 3.12.
Native execution also needs PostgreSQL 18 and Valkey 9. Follow
[local setup](docs/DEVELOPMENT.md#native-development) for the backend
working-directory `.env`, virtual environment and worker.

Backend commands, from `backend/` with its virtual environment active:

```bash
python -m pip check
python -m ruff check .
python -m pytest
```

Frontend commands, from `frontend/` after the documented `npm ci`:

```bash
npm audit --audit-level=moderate
npm run typecheck
npm run build
```

Documentation commands, from the repository root in a virtual environment:

```bash
python -m pip install --requirement requirements-docs.txt
python -m mkdocs build --strict
```

Run targeted checks first. The [CI guide](docs/CI.md) also covers OpenAPI/type
generation, clean database migrations, and no-cache container builds. Validate
links and filename case when editing documentation.

## Implementation rules

- Preserve the frontend/backend separation. After API changes, regenerate both
  contracts using the CI guide and inspect the generated diff.
- Provider adapters read, search and write; core matching owns evidence and
  decisions. Advertise only implemented capabilities and keep unsupported
  operations disabled.
- Preserve import/match/review/write, duplicate protection, preflight warnings,
  owner scoping, and provider batch/rate-limit behavior.
- Follow existing error types and structured API responses. Surface operational
  errors; do not silently convert failures into success.
- Use proper TypeScript types and guards. Python style is defined in
  `backend/pyproject.toml`; prefer existing helpers over parallel abstractions.
- For UI work, preserve tab roles, selected/panel relationships, roving focus,
  keyboard behavior and reduced-motion rules. Follow
  [accessibility guidance](ACCESSIBILITY.md).
- Tests and screenshots must use synthetic data and injected provider seams.
  Never use live provider credentials or real personal libraries for validation.

## Safety and delivery

Keep `.env`, provider tokens, MusicKit keys and session headers out of Git and
logs. Do not delete database/snapshot volumes as a test shortcut. PostgreSQL major
upgrades require the [documented migration](docs/POSTGRES_UPGRADE.md).

Preserve unrelated changes. Do not change policies, create releases, publish
images, or perform destructive operations without an explicit request. Build and
distribution details are in the
[development guide](docs/DEVELOPMENT.md#build-and-distribution).

Update directly affected docs. Do not claim checks, platform support, provider
behavior or accessibility conformance that you have not verified. For security
reports follow [SECURITY.md](SECURITY.md); for community conduct follow
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
