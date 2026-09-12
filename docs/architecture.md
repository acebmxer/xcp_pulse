# Architecture

[← back to the README](../README.md)

How the pieces fit together, and why. This page grows with each stage; today it
describes background jobs, redaction and log collection, and states the
decisions already taken about what follows.

## Shape

One container. A FastAPI application serving server-rendered Jinja templates,
with SQLite on a mounted volume. No JavaScript build step, no CDN, no broker —
the job queue is a table in the same database.

The image builds from `python:3.14-slim` ([Dockerfile](../Dockerfile)), which
is Debian 13 (trixie) running Python 3.14. That tag floats with upstream, so
the exact Debian point release moves independently of this project's version —
check the image at `docker run --rm ghcr.io/acebmxer/xcp_pulse:<tag> cat
/etc/os-release` if the exact point release matters.

Other facts about the image worth knowing before debugging or hardening it:

- **Runs as a non-root user**, `pulse` (uid 10001) — not root, and not the
  base image's default user.
- **Cannot install packages at runtime.** pip, setuptools and wheel are
  removed from the final image in the same build stage that creates it; only
  the build stage has them, and it is discarded.
- **Data lives on the `/data` volume** — the SQLite database and every
  collected/redacted artifact. Nothing outside that path survives a container
  recreate.
- **Listens on port 8080**, with a healthcheck against `GET /healthz` on that
  same port.

```
browser ──▶ uvicorn ──▶ FastAPI ──▶ SQLite  (/data/xcp-pulse.db)
                          │            ▲
                          │            │ queue + progress + metadata
                          │            │
                          └──▶ job worker thread
                                       │
                                       ├──▶ Xen Orchestra REST API
                                       │
                                       └──▶ artifact store  (/data/artifacts/)
```

## Why Python

The product is log processing: stream a 433 MB tarball, walk 603 members,
redact thousands of lines, extract by category. Python's `tarfile`, `gzip` and
`re` are standard library and stream natively.

The sibling project `install_xen_orchestra` is bash. That does not carry over —
bash cannot hold a session, serve a login page, or report progress on a
streaming download.

## Modules

| Module | Responsibility |
| --- | --- |
| `app/config.py` | Reads and validates the environment. The **only** module that touches `os.environ`. |
| `app/db.py` | SQLite connections and schema migrations. |
| `app/jobs.py` | The job queue: states, claiming, progress, cancellation. Owns the `jobs` table. |
| `app/job_runner.py` | The worker that runs queued jobs, and the registry of job kinds. |
| `app/job_inventory.py` | The **Refresh inventory** job — the worked example of a job. |
| `app/job_redact.py` | The **Redact artifact** job: masks a stored file, writes the report. |
| `app/job_collect.py` | The **Collect logs** job: downloads a host's bundle (and optionally its audit trail), redacting it unless that step was switched off, in which case `app/job_redact.py` handles it later. |
| `app/findings.py` | Turns Xen Orchestra API reads into findings: severity, title, evidence, action, source. |
| `app/job_findings.py` | The **API findings** job: runs the sources, stores the report as JSON and Markdown. |
| `app/job_log_findings.py` | The **log findings** job: reads a stored `*-logs.tgz` bundle, stores the report as JSON and Markdown. |
| `app/retention.py` | What stored collections to delete, always previewed before it acts. |
| `app/artifacts.py` | What a job produced: files on the volume, metadata in the database. |
| `app/redact.py` | The masking rules. The **only** place a redaction pattern is written. |
| `app/security.py` | Password hashing, sessions, login throttling. |
| `app/dependencies.py` | Shared route plumbing: the template environment, `login_required`. |
| `app/routes/` | HTTP endpoints, one module per area. |
| `app/main.py` | Builds the app and wires it together. |

Every public function is listed in [the function index](functions.md). Read it
before writing a new one.

## Background jobs

Work that takes time runs on a worker thread, and what it produced is kept.

**The queue is a database table, not an in-memory structure.** That one decision
is what buys everything else:

- a job survives a restart, and one interrupted by a restart is marked failed at
  the next start rather than showing as running for ever;
- the web request rendering progress reads rows, sharing no objects with the
  thread writing them;
- claiming a job is a conditional `UPDATE ... WHERE state = 'queued'`, which
  SQLite applies atomically — so a **separate worker process** can be added
  later as a second consumer of the same table, with no change to the schema,
  the queue module, or any job body.

**A thread rather than an asyncio task**, because the Xen Orchestra client is
synchronous `httpx` and the collection job later runs for 100 seconds. Awaiting
that on the event loop would freeze every other request; a thread leaves the UI
responsive without rewriting a client that works. The worker opens its own
SQLite connection — connections are not thread-safe to share — which WAL mode
makes concurrent with the request path.

**One worker.** Two concurrent 433 MB downloads would compete for the same disk
and the same XO instance for no gain, and a single worker makes "is a job of
this kind already running?" a question with an obvious answer.

**A running job is asked to stop, never killed.** A thread terminated mid-
download leaves a half-written file and an open connection. Cancellation is
recorded on the row, and the body notices it at its next progress report —
which is why reporting progress and checking for cancellation are the same
call.

## The artifact store

A job's output is a **file on the data volume**; only its metadata — name, media
type, size, SHA-256 — is a database row. The same store therefore holds the few
hundred bytes of JSON an inventory refresh writes and the 433 MB tarball
collection writes, so no second mechanism had to be built for the
large case, and SQLite never carries a blob.

Bodies live at `/data/artifacts/<job-id>/<artifact-id>`. Both ids are generated
here, and the operator-facing name lives in the row rather than in the path, so
a name coming from Xen Orchestra can never choose where a file is written.

## Why the inventory is stored rather than read per page

v0.3.0 read Xen Orchestra on every dashboard load. Since v0.4.0 the page shows
what the last successful **Refresh inventory** job stored.

The gain is not speed — those routes answer in milliseconds. It is that the
inventory becomes a *result with a time on it*: the page says how old it is, and
an XO that has gone away leaves the last known pools and hosts on screen with
the failure reported above them, instead of an error where the hosts were.

## Redaction

Masking is a **line-oriented transform**: `redact_line` takes a string and
returns a string. That is deliberate, because the caller that matters most is
not the preview page — it is the repack that will stream a 56 MB log through
this and must never hold it in memory. The preview page and the eventual bundle
therefore run identical rules over identical units.

**A placeholder keeps the shape of what it replaced.** `10.20.30.40` becomes
`[IPv4]`, and the same address gets the same placeholder everywhere, so a
support engineer reading a redacted log can still see that two lines concern one
host. Deleting the value instead would make the log safe and useless.

**Rule order is load-bearing**, and there are two cases where it decides the
answer rather than merely the label:

- `secret` runs before the value-shape rules, so `password=10.0.0.1` is masked
  as a password rather than as an address.
- `mac` runs before `ipv6`, because a MAC address is also a run of
  colon-separated hex and whichever rule runs first claims it.

**Some things are deliberately not masked.** Loopback, `0.0.0.0` and `localhost`
identify nobody, and masking them costs readability for no privacy. The hostname
rule matches only dotted names for the same reason: a bare-word rule would match
half the vocabulary of a log file, and an unreadable bundle helps nobody.

The IPv6 pattern is written as whole-address alternatives rather than "a run of
hex and colons", because the looser form matched the `12:30:45` timestamp that
prefixes nearly every syslog line.

## Authentication

A single admin user, whose Argon2id password hash comes from the environment.

Sessions are **rows in SQLite** referenced by a signed cookie. The signature
stops a client forging a session id; the row is what makes logout genuinely
invalidate a session rather than merely asking the browser to forget it. The
expiry slides forward on use.

Failed logins are recorded per address and counted inside a window. Being locked
out cannot be bypassed by then supplying the correct password.

## Database

SQLite in WAL mode, so reads do not block during the long writes that arrive
with log collection. Migrations are an **append-only list** in `app/db.py`, applied in order
at startup and recorded in `schema_version`. Editing an applied migration leaves
existing databases behind, so new changes always go on the end.

## The application is built lazily

`app/main.py` defines a module-level `__getattr__` so that importing the module
does not build the app. Constructing it reads the environment and creates the
data directory; doing that as a side effect of an import breaks tests and any
tooling that imports the module. uvicorn asks for `app.main:app`, and only then
is it built.

## Decisions already made for later stages

These come from measuring the real Xen Orchestra API against an XCP-ng 8.3 pool,
and they are why the stages are ordered as they are.

**The cached bundle is the source of truth.** `GET /hosts/{id}/logs.tgz` returns
433 MB in about 100 seconds, supports **no Range requests**, and ignores
filtering parameters — a request for one category cannot be made smaller. So
collection downloads once, caches, and every later product (redacted bundle,
category subset, findings) is derived from that cache. This is why per-category
collection must come *after* whole-bundle collection, not before.

**The API answers before anything is downloaded.** Findings from the API read
seven routes and need no `export:logs` privilege, so a pool's current state is
readable in seconds by an account that could never collect a bundle. That is why
API findings do not wait on collection, and why one refused source is recorded
rather than failing the run — a restricted account is refused the pool dashboard
and can still read messages and tasks.

**Xen Orchestra's `limit` returns the oldest records, not the newest**, and its
`sort` and `order` parameters are ignored. Event reads are therefore bounded by
a server-side `filter` on time, which also shrinks the response as the window
narrows. Messages and alarms carry seconds; tasks and backup runs carry
milliseconds — filtering one with the other's scale matches every record, so the
conversion lives in a single helper.

**Log findings read the cache, not Xen Orchestra.** The Findings page selects a
stored `*-logs.tgz` artifact produced by collection and queues `log_findings`.
The worker scans the archive locally, groups repeated matches by detected
condition, redacts representative evidence, and stores `log-findings.json` and
`log-findings.md` beside the job. It reports progress as it scans — driven off
bytes consumed against the bundle's own file size, one archive member at a
time, rather than the single 10 → 90 → 100 jump a whole-tar pass would
otherwise leave the page showing. A bundle that ends early — the same Nginx
Proxy Manager truncation the redacted repack already salvages — keeps whatever
was read before the break and marks the report `truncated` rather than failing
the job; an archive that cannot be read at all still fails it. API and log
reports remain separate until a later correlation layer can match timestamps,
host identity, and event details reliably.

**Redaction sits between the cache and anything sent onward.** Real bundles
contain internal addresses, usernames and session tokens, so the redacted copy
is produced by a line-oriented transform during repacking — a member at a time,
never the whole archive — and it is the copy the page presents as the one to
send. This is why redaction shipped before the first downloadable bundle.

The raw bundle is downloadable too, and the download list marks each copy as
**redacted — safe to send** or **raw — unmasked**. Withholding it would not make
the machine safer, since the file is already on the volume; what it would do is
leave an operator diagnosing their own pool without the unmasked original, which
is also the only thing that can answer "what was masked?" after the fact.

**Jobs before they are needed — done in v0.4.0.** A 101-second download needs
background execution, progress and cancellation. That machinery was built
against endpoints answering in milliseconds, so collection adds a slow job kind
to a proven system rather than inventing one under load.

**Compression is not worth it on the whole bundle.** Measured: 418 MB of the
433 MB is already-gzipped rotated logs, so repacking gains about 4%. The real
saving is dropping rotated history — current logs only, recompressed, come to
about 35 MB. That becomes the default shape of a support bundle.
