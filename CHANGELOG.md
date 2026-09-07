# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

XCP Pulse collects logs from XCP-ng hosts and Xen Orchestra through the
[Xen Orchestra REST API](https://docs.xen-orchestra.com/restapi).

## [Unreleased]

### Added

- **Redaction rules can be switched on and off.** Each rule on the redaction
  page now has a checkbox, and the choice is stored in the database rather than
  applying to one preview: a rule switched off stops masking everywhere
  redaction runs, and the preview shows what a collected bundle would actually
  get. Only the switched-off rules are recorded (new table
  `redaction_disabled`, migration 4), so a rule added to `RULES` in a later
  version is on from the moment it exists rather than needing a row written for
  it — storing the enabled set instead would have left every new rule silently
  inactive on existing installs. A stored name that no longer matches a rule is
  ignored, which makes renaming one safe. The page states how many rules are
  off, because a bundle collected with masking disabled is the failure this
  feature makes possible.

### Changed

- **`compose.yaml` is no longer tracked; `compose.yaml.example` is the
  committed template.** A compose file is a deployment's own — it carries the
  image tag, the port binding and any local overrides — so the repository ships
  the sample and each deployment keeps its own copy, matching how
  `xcp-pulse.env` has always worked. `compose.yaml` is now gitignored, and a
  test asserts both samples are committed and both working files are ignored.
  The download line in the README and in `docs/installation.md` fetches
  `compose.yaml.example` and writes it as `compose.yaml`; the clone path copies
  the sample before uncommenting `build: .`.

### Fixed

- **A failed read from Xen Orchestra was stored as a successful refresh with an
  empty inventory, so the dashboard told an administrator their account could
  see nothing.** The two collection readers in `app/xo_client.py` turned any
  non-200 response into an empty list, on the reasoning that a refused account
  and an empty installation are indistinguishable. That is true of `200 []`
  only. Every other status — a revoked token, a 502 from a reverse proxy, a
  rate limit — also became an empty list, which `inventory()` returned as a
  normal result, the job runner recorded as **succeeded**, and the dashboard
  rendered as "Nothing visible to this account". Observed against
  `xo-ce.pozzatech.com` with an admin token: one refresh stored
  `{"hosts": [], "pools": []}` and reported `0 pool(s), 0 host(s)`, while the
  refreshes either side of it read 1 pool and 2 hosts from the same unchanged
  connection. Both readers now share one status check and raise `XoError` for
  anything that is not a usable 200, so the failure reaches the job as an error
  and the last good inventory stays on screen.

- **The empty-inventory message asserted a cause the code had not
  established.** It told the operator the account "lacks read access", which is
  only one of the two things `200 []` can mean. It now names both possible
  causes and says they cannot be told apart from here.

## [0.5.0] - 2026-09-07

### Added

- **A redaction engine and a preview page, so what gets masked can be seen
  before anything is downloadable.** Eight rules cover IPv4 and IPv6 addresses,
  MAC addresses, UUIDs, `trackid=` session tokens, password/secret/session_id/
  API-key assignments, email addresses and dotted hostnames. Values keep their
  shape — `10.20.30.40` becomes `[IPv4]`, and equal values get equal
  placeholders, so a redacted log still shows that two lines mention the same
  host. Rule order is load-bearing: the secret rule runs before the value-shape
  rules so `password=10.0.0.1` is masked as a password, and the MAC rule runs
  before IPv6 because a MAC is also colon-separated hex. Loopback, `0.0.0.0`,
  `::1` and `localhost` are deliberately left legible, and the hostname rule
  matches only dotted names — a bare-word rule would match half the vocabulary
  of a log file. A new **Redaction** page pastes a snippet in and shows the
  result with per-rule hit counts; nothing pasted is stored. Redaction works a
  line at a time, which is the unit the streaming repack of a 56 MB log will
  use, so the same rules serve the preview and the eventual bundle.

  The hostname rule decides on an explicit suffix list rather than on shape.
  A first version matched anything dotted, which on a real XapiError paste
  masked the source filenames in the backtrace — `xapi_host.ml`, `index.mjs` —
  along with API method names like `host.setMaintenanceMode`, destroying the
  lines a support ticket exists to explain. Shape cannot separate them and
  neither can length, since `setMaintenanceMode` is longer than the longest
  real TLD. A host under an unlisted suffix is now missed rather than mangled,
  which is the safer way to be wrong: it is visible in the preview.

- **The image is published to GitHub Container Registry, so deploying no longer
  needs a clone.** `compose.yaml` pulls `ghcr.io/acebmxer/xcp_pulse` instead of
  building from the working directory, which means the compose file and an env
  file are a complete deployment — previously `build: .` required the Dockerfile
  and the whole `app/` tree to be present, and there was no published image to
  pull. A new workflow builds and pushes on a version tag, tagging `X.Y.Z`,
  `X.Y` and `latest`. Building from a clone still works: uncomment `build: .`
  and pass `--build`.

### Fixed

- **A stylesheet change reached the container but never the browser.**
  Starlette's `StaticFiles` sends an ETag and `Last-Modified` but no
  `Cache-Control`, so a browser is free to reuse a cached `style.css` without
  revalidating it — and does. Every CSS fix looked correct from the server side
  and had no effect on screen, which is indistinguishable from a fix that did
  not work. The stylesheet URL now carries the file's mtime as a query string,
  so each build is a URL no cache can match.

- **The example env file and `python -m app.hashpw` both told the user to write
  their settings into `.env`, which is the one name that cannot work.** Compose
  loads a file called `.env` as its own interpolation source, so the `$` in an
  Argon2 hash is eaten and the container starts with a mangled hash or refuses
  to start. Both now name `xcp-pulse.env`, matching what `compose.yaml` has
  always read.

## [0.4.0] - 2026-09-07

### Added

- **Work that takes time now runs in the background, and what it produced is
  kept.** A job has a state — queued, running, succeeded, failed or cancelled —
  with a percentage and a step description while it runs. A new **Jobs** page
  shows the history, live progress, and a Cancel button for anything running.

  **The queue is a database table rather than an in-memory structure**, which is
  what makes a job survive a restart and lets a web request read progress
  written by the worker thread without sharing objects with it. Claiming a job
  is a conditional `UPDATE ... WHERE state = 'queued'`, applied atomically by
  SQLite, so a separate worker process can be added later as a second consumer
  of the same table without changing the schema or any job body.

  Jobs run on a worker thread rather than as asyncio tasks: the Xen Orchestra
  client is synchronous `httpx` and log collection later runs for about 100
  seconds, which awaited on the event loop would freeze every other request. A
  running job is asked to stop rather than killed — a thread terminated
  mid-download leaves a half-written file — so cancellation is recorded and the
  body acts on it at its next progress report.

- **An artifact store for what a job produced.** Bodies are files under
  `/data/artifacts/`, with name, media type, size and SHA-256 in the database.
  The same store therefore holds the few hundred bytes of JSON an inventory
  refresh writes and the 433 MB bundle collection will write later, so no second
  mechanism is needed for the large case and SQLite never carries a blob. The
  operator-facing name is kept in the row rather than used as the filename, so a
  name coming from Xen Orchestra cannot choose where a file is written.

### Changed

- **The dashboard shows the inventory a Refresh job stored, rather than calling
  Xen Orchestra on every page load.** The gain is not speed — those routes
  answer in milliseconds — but that the inventory is now a result with a time
  attached: the page says how long ago it was read, and an unreachable Xen
  Orchestra leaves the last known pools and hosts on screen with the failure
  reported above them, instead of an error where the hosts were.

  The first load after saving a connection queues one refresh by itself, so a
  newly configured instance is not an empty page with no obvious next step.

- **A job recorded as running is marked failed at startup.** Such a row belongs
  to a thread that died with the previous process; left alone it would show as
  in progress indefinitely and, worse, block a new job of the same kind from
  being started.


## [0.3.0] - 2026-09-06

### Added

- **The dashboard lists the pools and hosts the connection can see.** Each pool
  is shown with its hosts beneath it — name, address, XCP-ng version, cores and
  memory in use — with the pool master and any disabled host marked. The
  inventory is read from Xen Orchestra on each page load rather than cached,
  which these routes answer fast enough to allow; the background job system
  arriving next is what makes caching worth having.

  **An empty inventory is explained rather than shown as an empty list.** Xen
  Orchestra answers an account without read privileges with `200` and an empty
  array, not a refusal, so a page showing no hosts would otherwise be
  indistinguishable from a healthy connection to an empty installation. Hosts
  whose pool is not visible are listed separately instead of being dropped,
  since pool and host read privileges are granted independently and an account
  can hold one without the other.

  `XoClient.inventory()` requests XO's `fields` parameter, which is what makes
  the collection routes return objects; without it they return href strings
  alone. The existing `list_pools`/`list_hosts` keep returning hrefs and are
  still what "Test connection" counts.

### Fixed

- **The function index recorded the 0.2.0 connection functions as
  unreleased.** Every row added for the Xen Orchestra connection work still
  carried `unreleased` in its "Since" column after 0.2.0 shipped, so the index
  understated what was in the last release.

- **The README test-count badge was out of date**, reading 27 against a
  suite of 76.

## [0.2.0] - 2026-09-06

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

[Unreleased]: https://github.com/acebmxer/xcp_pulse/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/acebmxer/xcp_pulse/releases/tag/v0.1.0
