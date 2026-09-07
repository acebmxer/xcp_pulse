# XCP Pulse

[![CI](https://github.com/acebmxer/xcp_pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/acebmxer/xcp_pulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/github/v/tag/acebmxer/xcp_pulse?label=version&sort=semver&color=brightgreen)](CHANGELOG.md)
[![Last commit](https://img.shields.io/github/last-commit/acebmxer/xcp_pulse)](https://github.com/acebmxer/xcp_pulse/commits)
[![Issues](https://img.shields.io/github/issues/acebmxer/xcp_pulse)](https://github.com/acebmxer/xcp_pulse/issues)
[![Stars](https://img.shields.io/github/stars/acebmxer/xcp_pulse)](https://github.com/acebmxer/xcp_pulse/stargazers)
[![Forks](https://img.shields.io/github/forks/acebmxer/xcp_pulse)](https://github.com/acebmxer/xcp_pulse/forks)
[![Unique cloners](https://img.shields.io/badge/unique%20cloners-0-lightgrey)](https://github.com/acebmxer/xcp_pulse/graphs/traffic)
[![Python](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)](compose.yaml)
[![Platform: Linux](https://img.shields.io/badge/platform-linux-333333?logo=linux&logoColor=white)](#requirements)
[![Tests](https://img.shields.io/badge/tests-137%20unit-informational)](https://github.com/acebmxer/xcp_pulse/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/badge/ruff-clean-brightgreen)](https://github.com/acebmxer/xcp_pulse/actions/workflows/ci.yml)

Collects XCP-ng and Xen Orchestra logs, bundles them for download, analyses
them, and reports findings — for your own troubleshooting or to attach to a
Vates support ticket.

> [!NOTE]
> XCP Pulse is being built in stages. **v0.4.0 connects to Xen Orchestra, lists
> your pools and hosts, and runs that as a background job with stored results.**
> Redaction is next, then log collection. See [the roadmap](docs/roadmap.md) for
> what is planned and what is done.

## Read next

| Page | What it covers |
| --- | --- |
| [Installation](docs/installation.md) | Getting the container running |
| [Configuration](docs/configuration.md) | Every setting, its default and what it does |
| [Architecture](docs/architecture.md) | How the pieces fit together |
| [Roadmap](docs/roadmap.md) | Current and upcoming features, with status |
| [Function index](docs/functions.md) | Every function in the codebase, in one place |
| [Contributing](CONTRIBUTING.md) | Development setup and conventions |
| [Security](SECURITY.md) | Threat model and reporting a vulnerability |

## Quick start

No clone and no build — the image is published to GHCR, so the compose file and
an env file are the whole deployment:

```bash
mkdir xcp-pulse && cd xcp-pulse
curl -O https://raw.githubusercontent.com/acebmxer/xcp_pulse/main/compose.yaml
curl -o xcp-pulse.env https://raw.githubusercontent.com/acebmxer/xcp_pulse/main/xcp-pulse.env.example

# Generate a password hash and paste it into xcp-pulse.env
docker compose run --rm xcp-pulse python -m app.hashpw

docker compose up -d
```

Then open <http://localhost:8080> and sign in. To build from source instead, see
[Installation](docs/installation.md).

> [!IMPORTANT]
> XCP Pulse refuses to start until `XCP_PULSE_ADMIN_PASSWORD_HASH` is set. It
> never falls back to a default password.

## Requirements

- Docker with Compose v2
- A Xen Orchestra instance reachable over HTTP(S) — once the XO connection ships
- Disk for collected bundles — roughly **450 MB per host per collection**, once
  log collection ships

## What it will do

Measured against a real XCP-ng 8.3 pool, so the numbers below are observed
rather than estimated:

- **Collect a host's full log bundle** via Xen Orchestra's REST API. A real
  bundle is **433 MB and takes about 100 seconds** — 603 files, all of `/var/log`.
- **Collect individual categories** — storage, XAPI, audit, security, kernel and
  the rest — extracted from the cached bundle without downloading again.
- **Redact** internal addresses, session tokens and credentials before anything
  leaves the machine. A single real `xensource.log` contained 8,359 lines
  matching password, secret or session patterns.
- **Report findings** — failed tasks, missing patches, storage errors, HA events
  — with the evidence behind each one.

## Configuration

The full list is in [docs/configuration.md](docs/configuration.md). The settings
you are most likely to change:

| Setting | Default | What it does |
| --- | --- | --- |
| `XCP_PULSE_ADMIN_USER` | `admin` | Login username |
| `XCP_PULSE_ADMIN_PASSWORD_HASH` | *(none)* | Argon2id hash; required |
| `XCP_PULSE_HTTPS` | `false` | Set true when served over HTTPS |
| `XCP_PULSE_SESSION_HOURS` | `12` | Session lifetime |

## Security

XCP Pulse holds credentials that can read every log on your pool, and stores
files containing session tokens and internal network topology. Run it on a
trusted management network behind a reverse proxy — not exposed to the internet.
See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).

XCP-ng and Xen Orchestra are projects of [Vates](https://vates.tech/). Xen
Orchestra is licensed under [AGPL-3.0](https://github.com/vatesfr/xen-orchestra).
XCP Pulse is an independent tool and is not affiliated with or endorsed by Vates.

## Credits

Built by [acebmxer](https://github.com/acebmxer). Sibling project:
[install_xen_orchestra](https://github.com/acebmxer/install_xen_orchestra).
