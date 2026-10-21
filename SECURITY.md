# Security policy

## Supported versions

Security fixes are supported on the current `main` branch and the
[latest stable release](https://github.com/mbianchidev/open-playlist-engine/releases/latest).
Prereleases and older stable releases are not separate supported security
branches. Older releases do not receive backported security fixes.

| Version | Security fixes |
|---|---|
| Current `main` | Supported |
| Latest stable release | Supported |
| Older releases | Not supported |

Maintenance, report responses and fixes are best effort. There is no guaranteed
acknowledgment, update or remediation deadline.

## Report a vulnerability privately

Use GitHub's enabled
[private vulnerability reporting](https://github.com/mbianchidev/open-playlist-engine/security/advisories/new)
for this repository. Do not report suspected vulnerabilities in public issues,
pull requests or discussions.

Include:

- the affected release/commit and deployment method;
- the affected endpoint or workflow, expected behavior and security impact;
- a minimal reproduction using synthetic data;
- relevant, sanitized logs and any suggested mitigation.

Never include live provider tokens, browser-session headers, MusicKit private
keys, database credentials, personal music libraries or customer data. Avoid
accessing anyone else's instance or provider account while investigating.

## Response and coordinated disclosure

Maintainers review reports privately, request clarification when needed, and
coordinate remediation and status updates through the private report. They may
ask you to verify a candidate fix using synthetic data.

Keep the report and reproduction private while maintainers and the reporter
agree on a fix and disclosure date. Coordinate public advisories and
security-sensitive release details before publishing them. If communication
stalls, follow up in the private report to revisit that plan; this policy does not
promise a fixed embargo period or remediation deadline.

## Operating an instance

The default self-host mode is intended for a trusted local environment. Follow
the [configuration guide](docs/CONFIGURATION.md) and
[public sharing setup](docs/PLAYLIST_SHARING.md) before exposing any service.
Keep `.env`, encryption keys and provider credentials private. Consult the
[upgrade guide](docs/POSTGRES_UPGRADE.md) before changing PostgreSQL major versions.

Ordinary bugs use the issue templates. Community conduct reports follow
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
