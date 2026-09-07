# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

XCP Pulse collects logs from XCP-ng hosts and Xen Orchestra through the
[Xen Orchestra REST API](https://docs.xen-orchestra.com/restapi).

## [Unreleased]

## [0.6.1] - 2026-09-07

### Changed

- **The dashboard's "What is coming" section listed eight shipped releases
  under a heading about future work.** The shipped stages have been removed
  from it; it now shows only what is planned, with the two findings items
  first because the Vates support package waits on them. The ordered group was
  re-checked against what has actually shipped: date ranges, individual log
  categories and findings from the API all had their prerequisite met by
  v0.2.0 and v0.6.0, so only the support package is still ordered.
  `docs/roadmap.md` was restructured to match — its "Next" section is gone,
  and findings-from-the-logs, which the dashboard had omitted entirely, is now
  listed.

### Fixed

- **The restricted-account warning on the Collect page showed for every
  connection, administrators included.** `collect.html` branched on
  `connection.is_admin`, an attribute `XoConnection` never had — it stores
  `account_type`, `"admin"` or `"restricted"`. Jinja resolves a missing
  attribute to Undefined, which is falsy, so `not connection.is_admin` was
  always true and the red "This connection uses a restricted account" box
  rendered above successful admin collections. `XoConnection` now has an
  `is_admin` property. The existing test asserted only that a restricted
  account is warned, which a warning stuck on satisfies too; a companion test
  now asserts the warning is absent for an admin connection.

- **The collection time estimate described the download, not the
  collection.** The Collect page, `README.md`, `docs/functions.md` and
  `job_collect.py` all said to expect about 100 seconds per host — the time
  `logs.tgz` takes to arrive. A collection also fetches the audit trail and
  redacts a copy of each; measured runs took 166 and 203 seconds. All four now
  say about three minutes, and the places that genuinely describe the
  `logs.tgz` transfer still say 100 seconds.

## [0.6.0] - 2026-09-07

### Added

- **Collecting a host's full log bundle, redacted and ready to send.** A new
  **Collect** page runs a per-host collection as a background job: it streams
  `logs.tgz` and `audit.txt` from Xen Orchestra to the data volume, keeps both
  raw copies, and produces a redacted copy of each. About 433 MB and 100
  seconds per host. The download is streamed a megabyte at a time and never
  held in memory; every chunk is a cancellation checkpoint, so Cancel stops a
  transfer rather than waiting for it, and a failed or cancelled download
  removes its partial file instead of leaving a half-written bundle to be
  mistaken for a collection.

  A tarball cannot be masked in place, so the redacted bundle is repacked
  member by member, each member's header size corrected — a placeholder rarely
  matches the length of what it replaced, and a stale size makes every later
  member unreadable. Compressed members are copied through unchanged, because
  masking gzip bytes rewrites the file and masks nothing. The masking is
  `redact.py`'s own `active_rules` and `Rule.apply` in the preview page's
  order, so what the preview shows is what a bundle gets.

  Both copies are kept and marked **redacted — safe to send** or **raw —
  unmasked**: the redacted copy is lossy, and the original is the only thing
  that can answer what was masked afterwards. Each collection writes a
  redaction report in the same shape the **Redact a stored file** job writes.

- **A retention policy, previewed before it acts.** The newest *n* collections
  are kept whatever their age, and only what remains is judged against an age
  limit, so a long gap in collecting cannot empty the store. The page shows
  which collections a cleanup would delete and how much space that returns
  before offering the button, and the cleanup re-plans as it runs so a
  collection finishing in between is accounted for. Individual collections can
  also be deleted outright.

- **A download route.** `GET /collect/download/{id}` streams an artifact from
  disk, so a 433 MB bundle is never read into memory to be returned. The
  browser's filename comes from the artifact row; the file on disk is a uuid,
  which keeps a host name from Xen Orchestra out of the filesystem.

### Fixed

- **A connection reset mid-download was reported as a bare errno, and threw
  away everything received.** The truncation handler caught
  `RemoteProtocolError` and `StreamClosed`, but a genuine TCP reset arrives as
  `httpx.ReadError` — verified against a socket closed with `SO_LINGER 0`,
  which raises `ReadError("[Errno 104] Connection reset by peer")`. That fell
  through to the generic handler, so the case where the bytes are most
  expensive to fetch again was the one that discarded them with a message
  reading as a local network fault. A reset is now reported as the upstream
  truncation it is, in the same words as the other two.

- **The retention pages said "1 collection(s)".** The preview, the stored-count
  line and the notice after a cleanup all carried the placeholder plural, on a
  page an operator reads before deleting gigabytes.

- **The delete button did nothing on a failed or cancelled collection.**
  `retention.delete_collection` looked the job up through `collections()`,
  which lists only *successful* jobs so that a half-written run is never
  counted as a stored result — but a failed collection is precisely what an
  operator wants to clear away, so the button posted, redirected and left the
  row on screen. It now looks the job up directly and refuses only a queued or
  running one, whose files the worker is still writing.

- **A truncated download reported a protocol error instead of naming the
  cause.** Measured against one pool: `logs.tgz` stops part-way, sometimes with
  the connection reset without ending the transfer, sometimes with an HTML
  error page appended after the archive bytes while the request still finishes
  HTTP 200 — so the status says success and only the tail gives it away. Both
  are now caught and reported as the truncation they are, with the places to
  look named, rather than an incomplete-chunked-read that reads as a network
  fault. Nothing partial is stored, and the last few kilobytes are scanned for
  the error page so a 433 MB body is still never held.

- **Documentation that had gone stale against shipped releases.** The README
  still required "a Xen Orchestra instance — once the XO connection ships",
  which shipped in v0.2.0; its status note named v0.5.2 after v0.5.3 was
  released; the Python badge said 3.13 while the image runs 3.14 and
  `pyproject.toml` requires 3.12 or newer, so it matched neither; the test
  badge counted 192 against a suite of 236. `docs/architecture.md` described
  itself as covering "v0.4.0 and the redaction work in progress" three
  redaction releases after it shipped, and `docs/roadmap.md` had no v0.5.3
  section, so its Shipped list stopped a release short. None of these change
  behaviour; all of them are read before anyone installs.

- **The build-from-source instructions generated the password hash from the
  published image, not the clone.** `docker compose run --rm xcp-pulse python
  -m app.hashpw` was given once, before the container exists, but on the clone
  path it resolves to `ghcr.io/acebmxer/xcp_pulse:latest` — verified with
  `docker compose config --images` against a copy of the sample. The hash
  itself is portable, so this worked by accident while silently pulling a
  release image; it fails for a clone that has changed `hashpw` or has no
  registry access. `docs/installation.md` now gives the overlay form of the
  command alongside it.

### Changed

- **A download that has stopped arriving is now treated as finished, not as a
  failure.** Measured against one pool through nginx: every byte of the bundle
  arrives and the response then never terminates, so the read blocked for the
  whole timeout with the complete file already on disk, and the job failed
  having thrown it away. A read timeout with data already received now ends the
  transfer and keeps what arrived, which is validated the same way a cleanly
  ended one is; with nothing received it is still reported as a real stall.
  The same download went from hanging 311 seconds and failing to returning in
  71 seconds with its bytes kept.

- **An error page appended by the host is trimmed off rather than failing the
  collection.** XCP-ng adds a few hundred bytes of HTML after the archive when
  its bundle build fails part-way, and the request still finishes HTTP 200.
  That was refused outright, which threw away 450 MB of readable logs over 263
  bytes of trailing markup; the page is now cut off and what remains is
  repacked as usual.

- **The redacted copy of a truncated bundle opened only read-only.** Archive
  tools reported it as corrupt. The repack copied the source's final member —
  the one the truncation lands in — by writing its header and then streaming
  the body, so the header promised more bytes than followed and the archive
  could not be read by random access, which is what every archive tool uses. A
  member whose body is short is now dropped rather than written, which is the
  same thing the source lost. Verified against a real 433 MiB truncated bundle:
  597 members, opening normally.

- **A truncated bundle no longer loses the whole collection.** The redacted
  copy is repacked by reading the archive as a stream rather than by random
  access, so a bundle whose end is missing — measured against one pool, the
  last member and the terminator absent — still yields a redacted copy of
  every member that arrived intact, and the job reports that the bundle ends
  early instead of failing with a bare `EOFError` and discarding a download
  that takes minutes to repeat. A bundle with nothing readable at all still
  fails, because an empty redacted copy sitting beside a raw one invites
  sending the wrong file.

- **Sizes are labelled in binary units.** `human_bytes` divides by 1024 but
  printed "MB", so a 454 MB download read as "426.0 MB" on the collect page —
  against a bundle the docs call 433 MB, which made a complete transfer look
  like a truncated one. It now prints KiB/MiB/GiB.

- **`wake_worker` is now shared route plumbing** in `app/dependencies.py`.
  Three routers had their own identical copy and a fourth was about to be
  written.

- **Byte counts are formatted in one place.** `artifacts.human_bytes` is now a
  module-level function that `Artifact.size_human` calls, because a total —
  a collection's files summed, a retention plan's freed space — has no
  `Artifact` to ask.

## [0.5.3] - 2026-09-07

### Changed

- **Building from a clone no longer means editing your own compose file.** The
  sample carried a commented `build: .` to uncomment, which is a change to the
  one file a deployment owns — easy to leave in place by accident, and it makes
  the sample awkward to copy over later. A committed `docker-compose.dev.yml`
  now adds `build: .` as an overlay, used alongside the main file
  (`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
  --build`), so the deployment file stays untouched and the built image keeps
  the tag it already names. This matches the layout `beacon_pxe` uses.

- **The compose sample now publishes the port on all interfaces, not just
  loopback.** It bound to `127.0.0.1:8080:8080`, so a deployment on a remote
  server started, reported healthy and served nothing — `http://<server>:8080`
  simply did not load, with no error to explain why, because the port answered
  only on the Docker host itself. Nothing in the logs shows this: the container
  always reports listening on `0.0.0.0:8080` internally regardless of what is
  published, and the healthcheck calls itself from inside. Installing on a
  machine other than your desktop is the normal case for this app, so the
  sample now uses `"8080:8080"` and keeps the loopback form as a commented
  alternative for a reverse proxy on the same host. `docs/installation.md` and
  `SECURITY.md` follow, and a troubleshooting entry explains how to tell the
  two `docker ps` mappings apart.

- **The compose sample now pulls `:latest` instead of a pinned version.** A
  fresh deployment had to have its image tag edited before it would run the
  current release, because the sample carried whatever version was current when
  it was written — a new user following the quick start got an old image and no
  indication of it. `:latest` is published on every release tag, so the sample
  is now correct without editing. The pinned form is kept as a commented
  example directly above it for anyone who wants to stay on one version, and
  the upgrade instructions in `docs/installation.md` cover both.

- **The compose sample is now `docker-compose.yml.example`, not
  `compose.yaml.example`.** Both names are ones Compose looks for on its own,
  so `docker compose up -d` behaves identically either way; the change is to
  the more widely recognised of the two, which is what most projects publish
  and what an operator expects to find. The README quick start, the
  installation and configuration docs, the `.gitignore` entry for the working
  copy and the compose tests all follow the new name. Upgrading an existing
  deployment needs nothing — an existing `compose.yaml` keeps working, and
  renaming it to `docker-compose.yml` is optional. **The env file is unchanged
  and must stay `xcp-pulse.env`:** Compose auto-loads a file named `.env` as
  its own interpolation source, which mangles the `$` characters in the Argon2
  password hash and stops the container from starting.

### Fixed

- **A flaky redaction-report test that failed CI on Python 3.12.** The jobs
  tests ran queued work through `run_pending_jobs`, whose docstring claimed the
  worker thread was never started in tests — but `create_app` starts one in its
  lifespan and `TestClient` runs that lifespan, so two consumers were competing
  for the same queue. When the background worker won the claim, the helper
  found nothing left to run and returned immediately, and the assertion read a
  page whose job had not finished. It passed locally and went red on CI, which
  is the failure a synchronous helper exists to prevent. `run_pending_jobs` now
  stops the app's worker before draining the queue, making every caller
  deterministic rather than patching the one test that happened to lose the
  race.

## [0.5.2] - 2026-09-07

### Added

- **A redaction report saying what was masked in a whole file.** A new
  "Redact a stored file" job (`app/job_redact.py`, kind `redact_artifact`)
  masks a stored artifact and keeps two results against the job: a redacted
  copy, named so the suffix stays last (`xensource.log` becomes
  `xensource.redacted.log`, so it still opens as a log), and
  `redaction-report.json` holding the per-rule hit counts for the run, the
  line count, and the name, size and SHA-256 of both files — without those
  hashes the counts have nothing to attach them to once the bundle is copied
  elsewhere. The jobs page renders the report as a table, with a switched-off
  rule shown as **off** rather than as zero hits, because "nothing was found"
  and "nothing was looked for" are the two answers a person about to send a
  bundle to Vates must be able to tell apart. Rules that matched nothing get a
  row too, for the same reason.

  The file is read a line at a time and never held in memory, which is what
  lets the same job serve a few hundred bytes of `inventory.json` now and a
  433 MB log bundle once collection lands. The masking itself is
  `app/redact.py`'s existing `active_rules` and `Rule.apply`, applied in the
  same order as `redact_text`, so the preview page and a real run cannot
  diverge; a test asserts the stored copy is byte-for-byte what `redact_text`
  produces on the same input. Line endings and malformed bytes both survive
  the round trip untouched — a redacted log whose CRLFs were rewritten no
  longer matches the file it came from, and one bad byte in a real log must
  not lose the whole run.

## [0.5.1] - 2026-09-07

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

[Unreleased]: https://github.com/acebmxer/xcp_pulse/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/acebmxer/xcp_pulse/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/acebmxer/xcp_pulse/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/acebmxer/xcp_pulse/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/acebmxer/xcp_pulse/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/acebmxer/xcp_pulse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/acebmxer/xcp_pulse/releases/tag/v0.1.0
