# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

XCP Pulse collects logs from XCP-ng hosts and Xen Orchestra through the
[Xen Orchestra REST API](https://docs.xen-orchestra.com/restapi).

## [Unreleased]

### Added

- **The Xen Orchestra connection can be configured and tested.** A settings page
  takes the XO address, an API token and the account type, and stores them in a
  new `xo_connection` table. The token is encrypted with AES-GCM under a key
  derived from the application secret key (`app/crypto.py`), so a copy of the
  database alone does not yield it; it is never rendered back to the page, which
  shows only that a token is stored. Changing `XCP_PULSE_SECRET_KEY` makes the
  stored token unreadable, and that is reported as needing the token re-entered
  rather than surfacing later as an authentication failure.

  **"Test connection" reports what the account can actually reach**, not merely
  that the request succeeded. This matters because Xen Orchestra answers an
  account with no privileges with `200` and an empty list rather than a refusal,
  so a connection that can see nothing is indistinguishable from a healthy one
  by status code alone. The result names the account type, the pools and hosts
  visible, and whether log collection will be possible — the last determined by
  reading the instance's own privilege catalogue, since whether `export:logs`
  can be granted is a property of the deployment.

  All Xen Orchestra calls go through `app/xo_client.py`; nothing else builds XO
  requests.

### Changed

- **Removed the remaining version numbers from unbuilt work.** The rule that
  only shipped work is numbered was recorded but never applied outside the
  roadmap, so `v0.2.0` and `v0.4.0` were still attached to planned features in
  `README.md`, `SECURITY.md`, the architecture, installation, configuration and
  function-index pages, four source-module docstrings, and the dashboard users
  see after logging in. Each now names the work — "once log collection ships",
  "the next piece of work" — rather than a number that was guessed.

- **Documented that log collection requires an admin Xen Orchestra account, and
  recorded that the connection settings will ask which type it has.** Verified
  against a live XO CE instance running `@xen-orchestra/rest-api` 0.39.0, not
  reasoned from the documentation, which disagrees with the behaviour.

  Downloading a host's logs requires `export:logs` on host, a distinct privilege
  from read — the instance's own API specification documents this, and a
  restricted account is refused with `403 not enough privileges` naming that
  action. But `export:logs` was absent from the privilege catalogue that roles
  are built from: it offered three host privileges (`read`, `allow-vm`, `*`)
  where the API requires eighteen. Of the eight built-in roles, only
  **Administrator** — via `host:*` — could reach the logs.

  The catalogue is stored per instance and Xen Orchestra does define
  `export:logs` in its source, so this is a property of the connected instance
  rather than a fixed rule, and the version string does not distinguish the two
  cases. XCP Pulse therefore reads the catalogue from Xen Orchestra and reports
  what that instance can actually grant.

  A restricted account remains sufficient for inventory and API-based findings.
  The connection settings will take the account type alongside the URL and token
  so this can be stated plainly instead of surfacing a bare `403`.

  `SECURITY.md` now treats the stored token as equivalent to pool admin
  credentials rather than read-only access, which changes where XCP Pulse should
  be run. Separately, role-based access control is an Essential+ feature on XOA,
  so lower tiers must use an admin account regardless; installations from the
  sources are unrestricted.

### Fixed

- **`docker compose up` mangled the admin password hash, and the container
  crash-looped.** An Argon2 hash is full of `$` characters
  (`$argon2id$v=19$m=65536,...`), and Compose treats `$` as variable
  interpolation in two independent places: once on values read from `env_file`,
  and again when it auto-loads `./.env` as the variable source for interpolating
  `compose.yaml` itself. The result was a stream of `"argon2id" variable is not
  set` warnings and a hash arriving with pieces missing, so the app refused to
  start — correctly, since it will not boot without a valid hash.

  Both halves are now handled: the env file is loaded with `format: raw`, and it
  is named `xcp-pulse.env` rather than `.env` so Compose does not auto-load it.
  `.env.example` is renamed to `xcp-pulse.env.example` to match, and `.env`
  stays gitignored so a file left over from the old layout cannot be committed.
  `tests/test_compose_env.py` asserts both halves stay in place.

  Upgrading from an earlier checkout: rename your `.env` to `xcp-pulse.env`.
  No other change is needed and the hash inside it is still valid.

### Added

- **Planned work is no longer given a version number.** Only shipped work has
  one; a number is assigned when the work actually starts. Numbering unbuilt
  work asserted an order that was mostly invented — of the sequence previously
  implied, only some steps are genuinely forced (redaction before any
  downloadable bundle, full collection before per-category extraction,
  published images before self-update), and the rest were numbered simply
  because they were appended to a list.

  The roadmap and the dashboard now group planned work as **any order** or
  **in this order**, and each item in the second group states what must come
  first and why. Reordering costs nothing, because there are no numbers to
  restate.

- The dashboard's **What is coming** list is a permanent part of the UI, not a
  placeholder. As stages ship its rows get updated rather than removed, and a
  test fails if the section disappears — so taking it out has to be a deliberate
  decision rather than a tidy-up.

- Four features added to the roadmap: date-range selection when collecting and
  downloading logs (v0.6.5), the documentation browsable in the web UI (v0.9.0),
  multiple user accounts with roles and an activity log (v0.10.0), and
  self-update from the UI (v1.1.0). The dashboard's stage list shows them too.

  The self-update entry records the approach used by the sibling project
  beacon_pxe — compare image digests read from the running container rather
  than version strings held in the database, and hand container recreation to a
  throwaway container outside the compose project, because a container cannot
  reliably replace itself. It also records the two prerequisites: a published
  image to compare a digest against, and the Docker socket mount, which is
  effectively host root and so is planned as opt-in.

## [0.1.0] - 2026-09-06

First release. Ships the container, the login screen and the empty dashboard —
the shell that later stages build on. There is no Xen Orchestra connection yet;
that arrives in 0.2.0.

### Added

- **Docker image and compose file.** A single `python:3.13-slim` container
  running uvicorn as a non-root user, with a named volume mounted at `/data`
  holding the SQLite database and the session signing key. The published port
  binds to `127.0.0.1` by default, because this app holds credentials that can
  read every log on the pool and should sit behind a reverse proxy.

- **Login screen with server-side sessions.** The admin password is supplied as
  an Argon2id hash in `XCP_PULSE_ADMIN_PASSWORD_HASH`, generated by
  `python -m app.hashpw`. The app refuses to start if the hash is missing or is
  not an Argon2 hash, and has no default password to fall back to. Sessions are
  rows in SQLite referenced by a signed cookie, so logging out invalidates a
  session immediately rather than only asking the browser to forget it.

- **Login throttling.** Five failed attempts from one address inside fifteen
  minutes blocks further attempts until the window passes, including attempts
  that supply the correct password. Both figures are configurable.

- **Function index** at `docs/functions.md`, listing every public function with
  its signature, what it does and who calls it. `tests/test_function_index.py`
  walks the AST of `app/` and fails when a function has no row, or when a row
  names a function that no longer exists — so the index cannot silently rot. It
  does not check the prose columns, because a check that can be satisfied by
  typing anything would imply a guarantee it does not give.

- **Documentation**: installation, configuration, architecture and roadmap
  pages, written alongside the code rather than after it.

### Notes

The Xen Orchestra REST API was probed against a live XCP-ng 8.3 pool while
planning this release, and three findings shaped the design of later stages:
`GET /hosts/{id}/logs.tgz` returns **433 MB in about 100 seconds**; it supports
**no Range requests and no server-side filtering**, so per-category collection
must extract from a locally cached bundle rather than making a smaller request;
and real bundles contain internal addresses and session tokens, which is why
redaction is scheduled before the first downloadable bundle rather than after.

[Unreleased]: https://github.com/acebmxer/xcp_pulse/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/acebmxer/xcp_pulse/releases/tag/v0.1.0
