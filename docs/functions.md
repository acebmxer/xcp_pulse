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
| Redact a stored file and report what was masked | `app/job_redact.py` |
| Collect a host's logs and redact them | `app/job_collect.py` |
| Ask the API what is wrong | `app/findings.py`, run by `app/job_findings.py` |
| Decide what stored collections to delete | `app/retention.py` — always `plan` before `apply` |
| Show a byte count on a page | `app/artifacts.py` — `human_bytes` |
| Store or read what a job produced | `app/artifacts.py` |
| Mask an address, token or credential out of text | `app/redact.py` |
| Read or repack a tarball | `app/job_collect.py` — `_redact_tarball` and its helpers |

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
| `count` | `(value: int) -> str` | A hit count with thousands separators, for a report column whose range spans six orders of magnitude | `jobs.html`, `collect.html`, `redaction.html`, as the `count` filter | unreleased |
| `counts_in` | `(text: str \| None) -> str` | Thousands-separates the integers in a stored progress line, leaving byte sizes alone | `jobs.html`, `collect.html`, as the `counts_in` filter | unreleased |
| `redirect` | `(url: str, status_code: int = 303) -> RedirectResponse` | Redirect, defaulting to see-other | `routes/auth` | v0.1.0 |
| `wake_worker` | `(request) -> None` | Tells the job worker to look now rather than at its next poll | every route that enqueues a job | v0.6.0 |
| `serve_artifact` | `(request, artifact_id: str, *, on_error: str) -> Response` | Streams one stored artifact to the browser, shared by every page that lists artifacts | `routes.collect.download_artifact`, `routes.jobs.download_job_artifact` | v0.6.3 |

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
| `XoClient.alarms` | `(since: float) -> list[dict]` | Alarms raised since a Unix time | `findings.collect_findings` | unreleased |
| `XoClient.backup_logs` | `(since: float) -> list[dict]` | Backup job runs since a Unix time | `findings.collect_findings` | unreleased |
| `XoClient.check_log_export` | `(*, is_admin: bool) -> LogExportSupport` | Whether this account can download host logs, and why not | `test_connection`, settings page | v0.2.0 |
| `XoClient.grantable_host_actions` | `() -> set[str]` | Host actions this instance can grant to a role | `check_log_export` | v0.2.0 |
| `XoClient.download_audit` | `(host_id, destination, *, on_chunk=None) -> int` | Streams a host's XAPI audit trail to a file | `job_collect.run` | v0.6.0 |
| `XoClient.download_logs` | `(host_id, destination, *, on_chunk=None) -> int` | Streams a host's log bundle to a file | `job_collect.run` | v0.6.0 |
| `XoClient.download_to` | `(path, destination, *, on_chunk=None) -> int` | Streams any XO route to a file, never holding the body | `download_logs`, `download_audit` | v0.6.0 |
| `XoClient.inventory` | `() -> Inventory` | Pools and hosts with their details, for the dashboard | `job_inventory.run` | v0.3.0 |
| `XoClient.is_admin` | `() -> bool` | Whether the account has XO administrator permission | `test_connection` | v0.2.0 |
| `XoClient.messages` | `(since: float) -> list[dict]` | XAPI messages since a Unix time | `findings.collect_findings` | unreleased |
| `XoClient.missing_patches` | `(pool_id: str) -> list[dict]` | Patches XO reports missing on one pool | `findings.collect_findings` | unreleased |
| `XoClient.pool_dashboard` | `() -> dict` | The dashboard totals: patches, backups, storage, host state | `findings.collect_findings` | unreleased |
| `XoClient.restore_logs` | `(since: float) -> list[dict]` | Restore runs since a Unix time | `findings.collect_findings` | unreleased |
| `XoClient.tasks` | `(since: float) -> list[dict]` | XO tasks since a Unix time, with failure results | `findings.collect_findings` | unreleased |
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

**The event routes are bounded by `filter`, never by `limit`.** Measured: XO
applies `limit` to the *oldest* records, so asking `/messages` for 2000 of 3472
rows returned the first month and hid every recent finding; `sort` and `order`
are accepted and ignored. `_events` builds the time filter for all five event
routes in one place, because messages and alarms carry seconds while tasks and
backup runs carry milliseconds — and filtering a millisecond field with a
seconds value matches everything, which looks exactly like a working filter.

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

## `app/job_redact.py` — the Redact artifact job

Masks one stored artifact into a redacted copy and writes a report of what was
masked. It reads a line at a time and never holds the file, which is what lets
the same job serve a few hundred bytes of `inventory.json` and the 433 MB log
bundle a collection writes.

The masking is `app/redact.py`'s — `active_rules` and `Rule.apply`, in the same
order as `redact_text` — so the preview page and a real run cannot diverge.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `build_report` | `(*, source, redacted, enabled, counts, lines) -> dict` | The report as plain JSON | `job_redact.run` | v0.5.2 |
| `existing_redaction` | `(conn, data_dir, artifact_id: str, enabled) -> Job \| None` | A finished redaction of this file with these same rules, or None | `routes.jobs.start_redaction` | v0.6.3 |
| `redacted_name` | `(name: str) -> str` | The name a redacted copy is stored under | `job_redact.run` | v0.5.2 |
| `report_from_job` | `(conn, data_dir, job_id: str) -> dict \| None` | Reads back the report a job stored | `routes.jobs` | v0.5.2 |
| `report_rows` | `(report: dict) -> list[dict]` | The report's per-rule rows, filled out from `RULES` | `routes.jobs` | v0.5.2 |
| `run` | `(context: JobContext) -> None` | Redacts the named artifact, storing the copy and the report | `job_runner`, via `register` | v0.5.2 |

Every rule appears in the report, including ones that matched nothing and ones
that were switched off — "was this masked?" is the question someone about to
send a bundle is asking, and a report listing only what fired cannot answer it.

`report_rows` fills each row from `RULES` where the stored report has no entry,
and keeps a stored rule this version no longer has, so an older report renders
without losing counts the run recorded.

## `app/job_collect.py` — the Collect logs job

Downloads one host's log bundle, keeps the raw copy, and writes a redacted copy.
Expect about **433 MB** and **two minutes** per host, measured on XCP-ng 8.3;
the 100-second figure quoted for the transfer is the `logs.tgz` download alone.
The XAPI audit trail is fetched as well only when the collection asks for it —
`xen-bugtool` already puts `/var/log/audit.log` into the bundle, and the
separate trail measured 770 MiB. With it, runs took 166 and 203 seconds.

The masking is `app/redact.py`'s — `active_rules` and `Rule.apply`, in the same
order as `redact_text` and `job_redact` — so the preview page, a redaction job
and a collection cannot mask differently.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `build_report` | `(*, host_id, host_name, enabled, counts, raw, redacted) -> dict` | The collection's redaction report as plain JSON | `job_collect.run` | v0.6.0 |
| `run` | `(context: JobContext) -> None` | Collects one host's logs, storing raw and redacted copies | `job_runner`, via `register` | v0.6.0 |

`build_report` extends `job_redact.build_report` rather than rebuilding the
rule rows, so a rule added to `RULES` appears in both reports without either
being edited — and the jobs and collect pages render both with one code path.

A tarball cannot be masked in place, so the redacted bundle is repacked member
by member: each text member is read a line at a time, and its header's size is
corrected because a placeholder rarely matches the length of what it replaced.
Compressed and oversized members are copied through unchanged.

The raw bundle is kept alongside the redacted one because the redacted copy is
lossy, and a question about what was masked can only be answered against the
original. The collect page marks which is which.

## `app/findings.py` — what the API says is wrong

Turns seven Xen Orchestra reads into findings: severity, title, evidence,
suggested action, source. No HTTP of its own — every call goes through
`XoClient`, and every finding is built by `_finding`, which is the only
constructor and redacts the evidence on the way in.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `collect_findings` | `(client, pools, *, enabled=None, window_days=30, now=None, progress=None) -> Report` | Reads every source and builds the report | `job_findings.run` | unreleased |
| `disabled_rule_titles` | `(enabled) -> list[str]` | The titles of the redaction rules switched off, for the report | `collect_findings` | unreleased |
| `sort_findings` | `(findings: list[Finding]) -> list[Finding]` | Worst first, then most recent, then by title | `collect_findings`, `job_findings.report_from_job` | unreleased |

`Finding`, `SourceInfo`, `SourceResult` and `Report` are dataclasses. `SOURCES`
holds each source's title, origin, what it holds and the unit it is counted in
— origin because Xen Orchestra *serves* all seven routes but *originates* only
three, and a XAPI message means log in to the host while a failed task means
look in XO. `SourceResult.examined_text` phrases the count, so a zero says what
was checked rather than reading as "nothing was examined". `Report.sources`
records every source that was tried, so a source that was **refused** is told
apart from one that was read and found nothing — the same distinction the
redaction report draws between a rule with no hits and a rule switched off.

**One source failing never fails the run.** A restricted account is refused the
pool dashboard and can still read messages and tasks, and a report covering
four sources is worth more than an error.

**Evidence is masked before it is stored**, by `redact.redact_line` under the
rules switched on at the time — not by a second masking implementation for
short strings. XO task `properties` carry usernames and client IP addresses,
so only `properties.name` and the failure message are read out of a task.

Message classification is two tables: `MESSAGE_RULES` matched exactly, then
`MESSAGE_PREFIX_RULES` matched by prefix for vendor families. Anything in
neither is routine and is dropped — measured, VM lifecycle events alone were
3,381 of 3,472 messages on one pool.

## `app/job_findings.py` — the API findings job

Runs `collect_findings` and stores the report twice: JSON, which the page reads
back, and Markdown, which is what goes into a support ticket.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `report_from_job` | `(conn, data_dir, job_id: str) -> Report \| None` | Rebuilds the Report a job stored | `routes.findings.findings_page` | unreleased |
| `run` | `(context: JobContext) -> None` | Reads every source and stores the report | `job_runner`, via `register` | unreleased |
| `to_markdown` | `(report: Report) -> str` | The report as Markdown, for a support ticket | `job_findings.run` | unreleased |
| `to_payload` | `(report: Report) -> dict` | The report as plain JSON | `job_findings.run` | unreleased |

`report_from_job` drops unknown keys and leaves missing ones at their dataclass
default, so a report written by an older version still renders.

The Markdown is **plain ASCII**. The file on disk is valid UTF-8 and is served
with `charset=utf-8`, and a real downloaded report still arrived with `â` where
its em dashes were — a report is emailed and opened by other people's tools, so
the reliable fix is to emit nothing that can mis-decode. `test_job_findings.py`
holds the generated document to ASCII.

The Markdown copy is written through `artifacts.store_file` rather than
`store_json`, which would wrap the text in JSON quotes. It is the one text
artifact written, and it still goes through the same store, hash and delete
path as a 433 MB bundle.

## `app/retention.py` — what to delete, previewed first

A collection is about 433 MB, so a data volume fills quickly. Every caller asks
`plan` before anything is deleted, and the page renders that plan — there is no
cleanup that has not been previewed.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `apply` | `(conn, data_dir, *, keep_days, keep_count) -> Plan` | Deletes what `plan` names, returning what actually went | `routes.collect.run_cleanup` | v0.6.0 |
| `collections` | `(conn) -> list[Collection]` | Every stored collection, newest first | `plan`, `delete_collection` | v0.6.0 |
| `delete_collection` | `(conn, data_dir, job_id: str) -> bool` | Deletes one collection outright, via `delete_job` | `routes.collect.delete_collection` | v0.6.0 |
| `delete_job` | `(conn, data_dir, job_id: str, *, kind: str \| None = None) -> bool` | Deletes one job and its files outright, optionally restricted to a kind | `retention.delete_collection`, `routes.jobs.delete_redaction` | v0.6.3 |
| `plan` | `(conn, *, keep_days, keep_count) -> Plan` | What a cleanup would delete, without deleting it | the collect page, `apply` | v0.6.0 |

Two limits apply together: the newest `keep_count` collections are kept
whatever their age, and only what remains is judged against `keep_days`. That
ordering is what stops a long gap in collecting from emptying the store.

`apply` re-plans at the moment it acts rather than trusting a plan posted back
from a page, so a collection finishing between the preview and the button is
accounted for.

## `app/artifacts.py` — what a job produced

Bodies are files on the data volume; only metadata is in the database. That
split is what lets one store hold both the JSON an inventory refresh writes and
the 433 MB tarball a collection writes.

The display name is stored in the row rather than used as the filename, so a
name coming from Xen Orchestra can never choose a path.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `artifact_path` | `(data_dir: Path, job_id: str, artifact_id: str) -> Path` | Where one artifact's body lives | `artifacts` internals, tests | v0.4.0 |
| `artifacts_dir` | `(data_dir: Path) -> Path` | The directory holding every body, created if absent | `artifacts.artifact_path` | v0.4.0 |
| `delete_for_job` | `(conn, data_dir: Path, job_id: str) -> int` | Removes a job's files and rows together | `retention.apply`, `retention.delete_collection` | v0.4.0 |
| `get_artifact` | `(conn, artifact_id: str) -> Artifact \| None` | One artifact's metadata | `routes.collect.download_artifact`, `routes.jobs` | v0.4.0 |
| `human_bytes` | `(size: int) -> str` | A byte count as something to put on a page | `Artifact.size_human`, retention, `job_collect` | v0.6.0 |
| `list_for_job` | `(conn, job_id: str) -> list[Artifact]` | Everything one job produced | `routes.jobs`, `job_inventory` | v0.4.0 |
| `read_json` | `(data_dir: Path, artifact: Artifact) -> object` | Reads a JSON artifact's body back | `job_inventory.inventory_from_job` | v0.4.0 |
| `store_file` | `(conn, data_dir, *, job_id, name, media_type, source, move=True) -> Artifact` | Takes a file on disk into the store, hashing in chunks | `job_collect`, `job_redact` | v0.4.0 |
| `store_json` | `(conn, data_dir, *, job_id, name, payload) -> Artifact` | Stores a JSON result | `job_inventory.run` | v0.4.0 |

`store_file` hashes by reading in chunks and moves rather than copies by
default, because the caller that matters most writes a 433 MB download to a
temporary path and has no reason to copy it again.

## `app/redact.py` — masking

Rules are applied in the order they appear in `RULES`, and the order is load-
bearing: `secret` before the value-shape rules so `password=10.0.0.1` is a
password, and `mac` before `ipv6` because a MAC is also colon-separated hex.

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `active_rules` | `(enabled: frozenset[str] \| set[str] \| None = None) -> tuple[Rule, ...]` | The rules to apply, in order; `None` means all | `redact_line`, `redact_text` | v0.5.0 |
| `enabled_rules` | `(conn: sqlite3.Connection) -> frozenset[str]` | The names of the rules currently switched on | `routes.redaction` | v0.5.1 |
| `redact_line` | `(line: str, enabled=None) -> str` | Masks one line — the unit a streaming repack uses | `redact_text`, `job_collect` | v0.5.0 |
| `redact_text` | `(text: str, enabled=None) -> tuple[str, dict[str, int]]` | Masks a block and counts hits per rule | `routes.redaction` | v0.5.0 |
| `rule_by_name` | `(name: str) -> Rule \| None` | One rule by name | `set_enabled_rules` | v0.5.0 |
| `set_enabled_rules` | `(conn: sqlite3.Connection, names: Iterable[str]) -> frozenset[str]` | Switches on exactly the named rules, off the rest | `routes.redaction` | v0.5.1 |

`Rule` is a frozen dataclass carrying the pattern, the placeholder and a `keep`
set of values not worth masking; `Rule.apply` returns the masked text and its
own hit count. `DEFAULT_ENABLED` is every rule — a rule is only off when
somebody turns it off.

Only the switched-off rules are stored (table `redaction_disabled`), so a rule
added to `RULES` in a later version is on from the moment it exists, including
on databases written before it did.

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
| `redaction_page` | `(request, username) -> Response` | `GET /redaction` — the preview page | router | v0.5.0 |
| `redaction_preview` | `(request, username, text) -> Response` | `POST /redaction` — masks pasted text and shows both | router | v0.5.0 |
| `redaction_rules_save` | `(request, username, rule: list[str]) -> Response` | `POST /redaction/rules` — stores which rules are on | router | v0.5.1 |
| `settings_delete` | `(request, username) -> Response` | `POST /settings/delete` — forgets the connection | router | v0.2.0 |
| `settings_page` | `(request, username) -> Response` | `GET /settings` — the XO connection page | router | v0.2.0 |
| `settings_save` | `(request, username, url, token, account_type, verify_tls) -> Response` | `POST /settings` — stores the connection | router | v0.2.0 |
| `settings_test` | `(request, username) -> Response` | `POST /settings/test` — tests and reports reach | router | v0.2.0 |
| `start_inventory_refresh` | `(request, username) -> Response` | `POST /jobs/refresh-inventory` — queues a refresh | router | v0.4.0 |
| `start_redaction` | `(request, username, artifact_id) -> Response` | `POST /jobs/redact` — queues a redaction of one stored file | router | v0.5.2 |
| `collect_page` | `(request, username, keep_days, keep_count) -> Response` | `GET /collect` — hosts, stored collections, retention preview | router | v0.6.0 |
| `delete_collection` | `(job_id, request, username) -> Response` | `POST /collect/{id}/delete` — deletes one collection | router | v0.6.0 |
| `download_artifact` | `(artifact_id, request, username) -> Response` | `GET /collect/download/{id}` — streams a stored file from disk | router | v0.6.0 |
| `delete_redaction` | `(job_id, request, username) -> Response` | `POST /jobs/{id}/delete` — deletes one redaction and its files | router | v0.6.3 |
| `download_job_artifact` | `(artifact_id, request, username) -> Response` | `GET /jobs/download/{id}` — streams a stored file from the jobs page | router | v0.6.3 |
| `run_cleanup` | `(request, username, keep_days, keep_count) -> Response` | `POST /collect/cleanup` — applies the retention limits | router | v0.6.0 |
| `start_collection` | `(request, username, host_id, include_audit) -> Response` | `POST /collect` — queues a collection for one host | router | v0.6.0 |
| `findings_page` | `(request, username) -> Response` | `GET /findings` — the latest stored findings report | router | unreleased |
| `start_findings` | `(request, username) -> Response` | `POST /findings` — queues a findings run | router | unreleased |
| `download_findings` | `(artifact_id, request, username) -> Response` | `GET /findings/download/{id}` — streams the stored JSON or Markdown | router | unreleased |

## `app/hashpw.py` — password hash helper

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `main` | `() -> int` | Prompts for a password, prints its hash | `python -m app.hashpw` | v0.1.0 |
