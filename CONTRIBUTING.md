# Contributing

Thanks for helping improve Open Playlist Engine. Bug fixes, provider adapters,
tests, accessibility improvements, and clearer documentation are welcome.

## Before you start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md).
- Search [existing issues](https://github.com/mbianchidev/open-playlist-engine/issues)
  before opening a bug report or feature proposal.
- Discuss substantial behavior, architecture, or provider changes in an issue
  before implementing them.
- Report suspected vulnerabilities through [SECURITY.md](SECURITY.md), not public
  issues. For conduct reports, use the contact in the Code of Conduct.
- Follow the [AI contribution policy](README.md#ai-policy). Contributors remain
  responsible for reviewing, testing, and explaining all their work.

**Coding agents must read [AGENTS.md](AGENTS.md) before editing.** It links to the
repository's architecture, development, testing, and safety guidance.

## Development and checks

The [development guide](docs/DEVELOPMENT.md) covers Python 3.12+, Node.js 26,
PostgreSQL 18, Valkey 9, local configuration, API/worker startup, and the repository
layout. The [CI guide](docs/CI.md) lists exact validation commands.

Use a fork and a feature branch for your changes. Keep the API and frontend
separate: the FastAPI contract and generated TypeScript schema are the boundary,
not shared application code.

Follow existing patterns:

- Python uses Ruff with a 100-character line limit and the rules in
  `backend/pyproject.toml`; run Ruff and the relevant pytest tests.
- Frontend changes must pass TypeScript checking and the production Vite build.
  Follow existing React components and theme tokens rather than adding a second
  UI system.
- Use synthetic data only. Mock provider HTTP with `httpx.MockTransport`, or use
  the injected YouTube Music client seam. Never record real libraries, customer
  data, credentials, or browser-session headers in tests or screenshots.
- Update the generated API contracts when routes or public models change, using
  the commands in the CI guide.
- Keep user/developer details in `docs/` and update navigation when adding a guide.

For UI changes, follow [ACCESSIBILITY.md](ACCESSIBILITY.md), verify keyboard and
narrow-screen behavior, and describe the checks you performed.

## Reports and proposals

Use the [issue chooser](https://github.com/mbianchidev/open-playlist-engine/issues/new/choose)
for bug reports and feature requests. Include a minimal reproduction, the version
or commit, deployment method, and expected versus actual behavior. Remove tokens,
account identifiers, and personal library data from logs.

Accessibility barriers can use the bug report template; include the affected
workspace, browser, viewport/zoom, and assistive technology when relevant.

## Pull requests

Keep each pull request focused. Explain the problem, the behavior change, and the
checks performed; link related issues where applicable. Include regression tests
when practical, and update relevant documentation or screenshots.

Use descriptive commit messages that explain the change.

Before requesting review, run the checks relevant to the changed surfaces and
inspect the diff for generated files, secrets, and unrelated edits. Address review
feedback and keep CI green. Reviews and maintenance are best effort; no turnaround
time is guaranteed.
