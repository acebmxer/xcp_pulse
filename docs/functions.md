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

Arriving in later stages, listed here so nobody starts a second one: an HTTP
client for Xen Orchestra and the job/artifact store (v0.2.0), the redaction
engine (v0.3.0), and tar handling (v0.4.0).

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

## `app/routes/` — HTTP endpoints

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `dashboard` | `(request, username) -> Response` | `GET /` — the landing page | router | v0.1.0 |
| `healthz` | `() -> dict[str, str]` | `GET /healthz` — unauthenticated liveness | router, compose healthcheck | v0.1.0 |
| `login_form` | `(request, next: str = "/") -> Response` | `GET /login` | router | v0.1.0 |
| `login_submit` | `(request, username, password, next) -> Response` | `POST /login` | router | v0.1.0 |
| `logout` | `(request) -> Response` | `POST /logout` | router | v0.1.0 |

## `app/hashpw.py` — password hash helper

| Function | Signature | Does | Used by | Since |
| --- | --- | --- | --- | --- |
| `main` | `() -> int` | Prompts for a password, prints its hash | `python -m app.hashpw` | v0.1.0 |
