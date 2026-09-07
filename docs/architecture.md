# Architecture

[← back to the README](../README.md)

How the pieces fit together, and why. This page grows with each stage; today it
describes v0.1.0 and states the decisions already taken about what follows.

## Shape

One container. A FastAPI application serving server-rendered Jinja templates,
with SQLite on a mounted volume. No JavaScript build step, no CDN, no separate
worker or broker.

```
browser ──▶ uvicorn ──▶ FastAPI ──▶ SQLite  (/data/xcp-pulse.db)
                          │
                          └──▶ Xen Orchestra REST API   (planned)
                                        │
                                        └──▶ artifact store  (planned)
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
| `app/security.py` | Password hashing, sessions, login throttling. |
| `app/dependencies.py` | Shared route plumbing: the template environment, `login_required`. |
| `app/routes/` | HTTP endpoints, one module per area. |
| `app/main.py` | Builds the app and wires it together. |

Every public function is listed in [the function index](functions.md). Read it
before writing a new one.

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

**Redaction sits between the cache and anything downloadable.** Real bundles
contain internal addresses, usernames and session tokens. The raw tarball is
never served directly; downloads are produced by a streaming, line-oriented
transform during repacking, so a 56 MB log is never held in memory. This is why
redaction is scheduled before the first downloadable bundle.

**Jobs before they are needed.** A 101-second download needs background
execution, progress and cancellation. That machinery is introduced with the XO
connection, against endpoints that answer in milliseconds, so collection later
adds a slow job type to a proven system rather than inventing it under load.

**Compression is not worth it on the whole bundle.** Measured: 418 MB of the
433 MB is already-gzipped rotated logs, so repacking gains about 4%. The real
saving is dropping rotated history — current logs only, recompressed, come to
about 35 MB. That becomes the default shape of a support bundle.
