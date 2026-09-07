# Function index

[← back to the README](../README.md)

Every public function in `app/`, in one place, so the same thing does not get
written three different ways.

**Read this before writing a new function.** If something here already does the
job, call it. If you genuinely need a second implementation, say so in its
`Does` column and in a comment on the function itself — *deliberately not
`<the other one>`, because `<reason>`* — so the next reader can tell a decision
from an accident.

`tests/test_function_index.py` checks that every public function has a row and
that no row names a function that has been removed. It does not check the prose
columns; those are a reviewer's job.

---

## Where to look first

| I need to… | Look at |
| --- | --- |
| Read a setting | `app/config.py` — the only module that reads `os.environ` |
| Talk to the database | `app/db.py` |
| Hash or check a password | `app/security.py` |
| Create, read or end a session | `app/security.py` |
| Throttle or count login failures | `app/security.py` |
| Require a login on a route | `app/dependencies.py` — `login_required` |
| Redirect after a POST | `app/dependencies.py` — `redirect` |
| Render a page | `app/dependencies.py` — `templates` |
| Add a route | `app/routes/` — one module per area |
| Call the Xen Orchestra API | `app/xo_client.py` — the only module that talks to XO |
| Read or store the XO connection | `app/xo_connection.py` |
| Encrypt or decrypt a stored secret | `app/crypto.py` |
| Queue, read or cancel a background job | `app/jobs.py` |
| Add a new kind of background job | `app/job_inventory.py` as the worked example; register it in `app/job_runner.py` and import it in `app/main.py` |
| Store or read what a job produced | `app/artifacts.py` |

Arriving in later stages, listed here so nobody starts a second one: the
redaction engine and tar handling.

---

## `app/config.py` — settings

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `load_settings` | `() -> Settings` | Reads and validates the environment | `main.create_app` | v0.1.0 |

`Settings` is a frozen dataclass; `ConfigError` is raised when a setting is
missing or unusable and the app must not start.

## `app/db.py` — SQLite

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `connect` | `(db_path: Path) -> Connection` | Opens a connection with the required pragmas | `db.init_db` | v0.1.0 |
| `current_version` | `(conn) -> int` | Returns the applied schema version | `db.migrate` | v0.1.0 |
| `init_db` | `(db_path: Path) -> Connection` | Opens the database and migrates it | `main.lifespan` | v0.1.0 |
| `migrate` | `(conn) -> int` | Applies pending migrations | `db.init_db` | v0.1.0 |
| `transaction` | `(conn) -> Iterator[Connection]` | Context manager; commits or rolls back | `db.migrate` | v0.1.0 |

Migrations are append-only. Editing an applied one leaves existing databases
behind.

## `app/security.py` — authentication

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `clear_login_failures` | `(conn, ip: str) -> None` | Forgets an address's failures after success | `routes/auth.login_submit` | v0.1.0 |
| `client_ip` | `(request) -> str` | Best-effort client address for throttling | `routes/auth.login_submit` | v0.1.0 |
| `create_session` | `(conn, username: str, session_hours: int) -> str` | Records a session, returns its id | `routes/auth.login_submit` | v0.1.0 |
| `current_user` | `(request) -> str \| None` | Resolves the cookie to a username | `dependencies.login_required`, `routes/auth` | v0.1.0 |
| `destroy_session` | `(conn, session_id: str) -> None` | Deletes a session immediately | `routes/auth.logout`, `security.get_session_user` | v0.1.0 |
| `get_session_user` | `(conn, session_id: str) -> str \| None` | Username for a live session | `security.current_user` | v0.1.0 |
| `hash_password` | `(plain: str) -> str` | Argon2id hash for storage | `app/hashpw.py`, tests | v0.1.0 |
| `is_rate_limited` | `(conn, ip, max_attempts, window_minutes) -> bool` | True when an address has failed too often | `routes/auth.login_submit` | v0.1.0 |
| `login_failure_count` | `(conn, ip: str, window_minutes: int) -> int` | Counts failures inside the window | `security.is_rate_limited` | v0.1.0 |
| `purge_expired_sessions` | `(conn) -> int` | Deletes expired sessions | `main.lifespan` | v0.1.0 |
| `purge_old_login_attempts` | `(conn, window_minutes: int) -> int` | Drops attempts older than the window | `main.lifespan` | v0.1.0 |
| `record_login_failure` | `(conn, ip: str) -> None` | Records a failed login | `routes/auth.login_submit` | v0.1.0 |
| `sign_session_id` | `(session_id: str, secret_key: str) -> str` | Wraps a session id in a signed value | `routes/auth` | v0.1.0 |
| `touch_session` | `(conn, session_id: str, session_hours: int) -> None` | Slides the expiry forward on use | `security.current_user` | v0.1.0 |
| `unsign_session_id` | `(cookie_value: str, secret_key: str) -> str \| None` | Recovers an id, None if forged | `security.current_user`, `routes/auth.logout` | v0.1.0 |
| `verify_password` | `(plain: str, stored_hash: str) -> bool` | Checks a password against its hash | `routes/auth.login_submit` | v0.1.0 |

`SESSION_COOKIE` is the cookie name. Sessions are stored server-side so logout
genuinely invalidates rather than merely asking the browser to forget.

## `app/dependencies.py` — shared route plumbing

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `login_required` | `(request) -> str` | FastAPI dependency; 303s anonymous callers | every protected route | v0.1.0 |
| `age` | `(timestamp: float \| None) -> str` | A timestamp as how long ago it was, for a stored result | `dashboard.html`, as the `age` filter | v0.4.0 |
| `redirect` | `(url: str, status_code: int = 303) -> RedirectResponse` | Redirect, defaulting to see-other | `routes/auth` | v0.1.0 |

`templates` is the shared Jinja environment; `RedirectToLogin` is the exception
`login_required` raises, handled in `main.create_app`.

## `app/logging_conf.py` — application logging

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `configure_logging` | `(level: str = "INFO") -> Logger` | Sends app logs to stdout | `main.lifespan` | v0.1.0 |

This is XCP Pulse's own diagnostics, not the XCP-ng logs it collects.

## `app/main.py` — application assembly

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `create_app` | `(settings: Settings \| None = None) -> FastAPI` | Builds and wires the app | module scope, tests | v0.1.0 |
| `lifespan` | `(app: FastAPI)` | Opens the database on start, closes on stop | `main.create_app` | v0.1.0 |

## `app/crypto.py` — encryption at rest

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `decrypt` | `(stored: str, secret_key: str) -> str` | Recovers a stored secret; raises on a wrong key | `xo_connection.build_client` | v0.2.0 |
| `encrypt` | `(plaintext: str, secret_key: str) -> str` | Encrypts a secret for storage, base64 out | `xo_connection.save_connection` | v0.2.0 |

The key is derived from the application secret key, so a copy of the database
alone does not decrypt. `DecryptionError` means the key changed or the value was
altered — both need the token entering again.

## `app/xo_client.py` — Xen Orchestra REST API

Every call to Xen Orchestra goes through here. Nothing else builds XO requests.

These are methods on `XoClient`, so they carry no row above — the table and its
checker cover top-level functions. Build one with
`xo_connection.build_client()` rather than constructing it by hand, so the
stored URL, token and TLS setting are applied in one place.

| Method | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `XoClient.check_log_export` | `(*, is_admin: bool) -> LogExportSupport` | Whether this account can download host logs, and why not | `test_connection`, settings page | v0.2.0 |
| `XoClient.grantable_host_actions` | `() -> set[str]` | Host actions this instance can grant to a role | `check_log_export` | v0.2.0 |
| `XoClient.inventory` | `() -> Inventory` | Pools and hosts with their details, for the dashboard | `job_inventory.run` | v0.3.0 |
| `XoClient.is_admin` | `() -> bool` | Whether the account has XO administrator permission | `test_connection` | v0.2.0 |
| `XoClient.list_hosts` | `() -> list[str]` | Host hrefs this account can see, for counting only | `test_connection` | v0.2.0 |
| `XoClient.list_pools` | `() -> list[str]` | Pool hrefs this account can see, for counting only | `test_connection` | v0.2.0 |
| `XoClient.test_connection` | `() -> ConnectionTest` | Checks URL and token, reports what the account reaches | `routes.settings.settings_test` | v0.2.0 |

`list_pools` and `list_hosts` return href strings and exist only to count what
is visible. `inventory` asks the same routes for `fields`, which is what makes
XO return objects rather than hrefs, and is what the dashboard renders.

`XoError` carries a message written for the operator; the settings page renders
it directly. `ConnectionTest` and `LogExportSupport` are frozen dataclasses.

**A restricted account gets HTTP 200 and an empty list from `/pools` and
`/hosts`, not a 403.** Treating a successful request as a usable connection is
therefore wrong, which is why `test_connection` reports what was visible rather
than only whether the call succeeded.

## `app/xo_connection.py` — the stored connection

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `build_client` | `(conn, secret_key: str) -> XoClient` | Builds a client from the stored connection | `routes.settings.settings_test`, `job_inventory.run` | v0.2.0 |
| `delete_connection` | `(conn) -> bool` | Removes the connection and its token | `routes.settings.settings_delete` | v0.2.0 |
| `get_connection` | `(conn) -> XoConnection \| None` | Reads the connection, never the token | `routes.settings` | v0.2.0 |
| `record_test_result` | `(conn, *, ok: bool, message: str) -> None` | Remembers the last test outcome | `routes.settings.settings_test` | v0.2.0 |
| `save_connection` | `(conn, *, url, token, account_type, verify_tls, secret_key) -> None` | Stores the connection, encrypting the token | `routes.settings.settings_save` | v0.2.0 |

`get_connection` deliberately does not return the token: the settings template
renders this object, and shows only that a token is stored.

## `app/jobs.py` — the job queue

The queue is a database table, not an in-memory structure. That is what lets a
job survive a restart, lets a web request read progress a worker thread wrote,
and lets a separate worker process become a second consumer later without the
schema changing.

Nothing outside this module writes to the `jobs` table. A running job reports
through the `JobContext` it is handed.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `claim_next` | `(conn) -> Job \| None` | Atomically takes the oldest queued job and marks it running | `job_runner.JobWorker.run_one` | v0.4.0 |
| `enqueue` | `(conn, kind: str, params: dict \| None = None) -> Job` | Adds a job to the queue | `routes.jobs`, `routes.dashboard` | v0.4.0 |
| `get_job` | `(conn, job_id: str) -> Job \| None` | One job by id | `routes.jobs`, `jobs.request_cancel` | v0.4.0 |
| `has_active` | `(conn, kind: str) -> bool` | True when a job of this kind is queued or running | `routes.jobs`, `routes.dashboard` | v0.4.0 |
| `latest_job` | `(conn, kind: str) -> Job \| None` | The newest job of a kind, whatever its state | `routes.dashboard` | v0.4.0 |
| `latest_successful` | `(conn, kind: str) -> Job \| None` | The newest job of a kind that succeeded | `routes.dashboard` | v0.4.0 |
| `list_jobs` | `(conn, *, kind: str \| None = None, limit: int = 50) -> list[Job]` | Recent jobs, newest first | `routes.jobs.jobs_page` | v0.4.0 |
| `mark_cancelled` | `(conn, job_id: str) -> None` | Records that a job stopped on request | `job_runner.JobWorker.run_one` | v0.4.0 |
| `mark_failed` | `(conn, job_id: str, error: str) -> None` | Records a failure and its reason | `job_runner.JobWorker.run_one` | v0.4.0 |
| `mark_succeeded` | `(conn, job_id: str, step: str = "") -> None` | Records completion | `job_runner.JobWorker.run_one` | v0.4.0 |
| `request_cancel` | `(conn, job_id: str) -> bool` | Asks a job to stop; True if it was still active | `routes.jobs.cancel_job` | v0.4.0 |
| `reset_orphans` | `(conn) -> int` | Fails jobs left running by a stopped process | `main.lifespan` | v0.4.0 |

`Job` and `JobContext` are dataclasses; `JobCancelled` is what unwinds a body
that has been asked to stop. `JobContext.progress()` is both the progress
report and the cancellation checkpoint, so a body that reports progress is
cancellable without having to think about it.

A running job is **never killed from outside** — a thread stopped mid-download
leaves a half-written file and an open connection. Cancellation is recorded and
the body notices it where stopping is safe.

## `app/job_runner.py` — running queued jobs

One worker thread, consuming the queue above. A thread rather than an asyncio
task because the XO client is synchronous httpx and the collection job later
runs for 100 seconds — awaiting that on the event loop would freeze the UI.

**This is one implementation of a consumer, not the design.** Jobs are claimed
with an atomic conditional UPDATE, so a separate worker *process* can be added
without changing `app/jobs.py`, the schema, or any job body.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `register` | `(kind: str, body: Callable[[JobContext], None]) -> None` | Makes a job kind runnable | each job module at import | v0.4.0 |
| `registered_kinds` | `() -> list[str]` | Every runnable job kind | tests, logging | v0.4.0 |

`JobWorker` owns the thread. `JobWorker.run_one(conn)` is the whole of what it
does per job and is public so tests can run a job without a background thread
racing their assertions. Registration happens at import, so a module defining a
kind must be imported in `main.py` or its jobs fail with "no handler".

## `app/job_inventory.py` — the Refresh inventory job

The worked example of a job, and what the dashboard reads. Deliberately built
against endpoints answering in milliseconds, so the queue, progress,
cancellation and the artifact store are proven before the 100-second collection
job lands on them.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `inventory_from_job` | `(conn, data_dir, job_id: str) -> Inventory \| None` | Rebuilds the Inventory a job stored | `routes.dashboard.dashboard` | v0.4.0 |
| `run` | `(context: JobContext) -> None` | Reads XO and stores the inventory as an artifact | `job_runner`, via `register` | v0.4.0 |

`inventory_from_job` drops unknown keys and leaves missing ones at their
dataclass default, so an artifact written by an older version still loads.

## `app/artifacts.py` — what a job produced

Bodies are files on the data volume; only metadata is in the database. That
split is what lets one store hold both the JSON an inventory refresh writes and
the 433 MB tarball collection will write later.

The display name is stored in the row rather than used as the filename, so a
name coming from Xen Orchestra can never choose a path.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `artifact_path` | `(data_dir: Path, job_id: str, artifact_id: str) -> Path` | Where one artifact's body lives | `artifacts` internals, tests | v0.4.0 |
| `artifacts_dir` | `(data_dir: Path) -> Path` | The directory holding every body, created if absent | `artifacts.artifact_path` | v0.4.0 |
| `delete_for_job` | `(conn, data_dir: Path, job_id: str) -> int` | Removes a job's files and rows together | retention, later stages | v0.4.0 |
| `get_artifact` | `(conn, artifact_id: str) -> Artifact \| None` | One artifact's metadata | later download routes | v0.4.0 |
| `list_for_job` | `(conn, job_id: str) -> list[Artifact]` | Everything one job produced | `routes.jobs`, `job_inventory` | v0.4.0 |
| `read_json` | `(data_dir: Path, artifact: Artifact) -> object` | Reads a JSON artifact's body back | `job_inventory.inventory_from_job` | v0.4.0 |
| `store_file` | `(conn, data_dir, *, job_id, name, media_type, source, move=True) -> Artifact` | Takes a file on disk into the store, hashing in chunks | collection, later | v0.4.0 |
| `store_json` | `(conn, data_dir, *, job_id, name, payload) -> Artifact` | Stores a JSON result | `job_inventory.run` | v0.4.0 |

`store_file` hashes by reading in chunks and moves rather than copies by
default, because the caller that matters most writes a 433 MB download to a
temporary path and has no reason to copy it again.

## `app/routes/` — HTTP endpoints

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `cancel_job` | `(job_id, request, username) -> Response` | `POST /jobs/{id}/cancel` — asks a job to stop | router | v0.4.0 |
| `dashboard` | `(request, username) -> Response` | `GET /` — shows the inventory the last refresh stored | router | v0.1.0 |
| `healthz` | `() -> dict[str, str]` | `GET /healthz` — unauthenticated liveness | router, compose healthcheck | v0.1.0 |
| `login_form` | `(request, next: str = "/") -> Response` | `GET /login` | router | v0.1.0 |
| `login_submit` | `(request, username, password, next) -> Response` | `POST /login` | router | v0.1.0 |
| `job_status` | `(job_id, request, username) -> Response` | `GET /jobs/{id}/status` — one job's state as JSON | router | v0.4.0 |
| `jobs_page` | `(request, username) -> Response` | `GET /jobs` — history, progress and starting a refresh | router | v0.4.0 |
| `logout` | `(request) -> Response` | `POST /logout` | router | v0.1.0 |
| `settings_delete` | `(request, username) -> Response` | `POST /settings/delete` — forgets the connection | router | v0.2.0 |
| `settings_page` | `(request, username) -> Response` | `GET /settings` — the XO connection page | router | v0.2.0 |
| `settings_save` | `(request, username, url, token, account_type, verify_tls) -> Response` | `POST /settings` — stores the connection | router | v0.2.0 |
| `settings_test` | `(request, username) -> Response` | `POST /settings/test` — tests and reports reach | router | v0.2.0 |
| `start_inventory_refresh` | `(request, username) -> Response` | `POST /jobs/refresh-inventory` — queues a refresh | router | v0.4.0 |

## `app/hashpw.py` — password hash helper

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `main` | `() -> int` | Prompts for a password, prints its hash | `python -m app.hashpw` | v0.1.0 |
