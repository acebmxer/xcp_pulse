"""SQLite access and schema management.

One connection factory, one schema definition, one migration path. Later
stages add tables here (xo_connection for the XO link; jobs and artifacts for
collection) by appending a migration, never by editing an applied one.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Each entry is applied in order and recorded in schema_version. Append only:
# editing an applied migration leaves existing databases behind.
_MIGRATIONS: list[str] = [
    # 0 -> 1: sessions and login throttling.
    """
    CREATE TABLE sessions (
        id          TEXT PRIMARY KEY,
        username    TEXT NOT NULL,
        created_at  REAL NOT NULL,
        expires_at  REAL NOT NULL
    );
    CREATE INDEX idx_sessions_expires ON sessions (expires_at);

    CREATE TABLE login_attempts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ip          TEXT NOT NULL,
        attempted_at REAL NOT NULL
    );
    CREATE INDEX idx_login_attempts_ip_time ON login_attempts (ip, attempted_at);
    """,
    # 1 -> 2: the Xen Orchestra connection.
    #
    # A single row, pinned by a CHECK to id = 1: XCP Pulse talks to one XO at a
    # time, and enforcing that in the schema means no code path has to decide
    # which of several rows is current.
    #
    # The token is stored encrypted (see app/crypto.py); the column name says so
    # to stop anything writing a plaintext token into it.
    """
    CREATE TABLE xo_connection (
        id                  INTEGER PRIMARY KEY CHECK (id = 1),
        url                 TEXT NOT NULL,
        token_encrypted     TEXT NOT NULL,
        account_type        TEXT NOT NULL,
        verify_tls          INTEGER NOT NULL DEFAULT 1,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL,
        last_tested_at      REAL,
        last_test_ok        INTEGER,
        last_test_message   TEXT
    );
    """,
    # 2 -> 3: background jobs and the artifacts they produce.
    #
    # The queue lives in the database, not in the worker's memory. That is what
    # lets a job survive a restart, lets the UI read progress written by another
    # thread without sharing objects, and lets a separate worker process become
    # a second consumer later without the schema changing. A worker claims a job
    # with a conditional UPDATE on state, which SQLite applies atomically, so
    # two consumers cannot take the same row.
    #
    # state is one of queued/running/succeeded/failed/cancelled — see
    # app/jobs.py, which is the only module that writes it.
    """
    CREATE TABLE jobs (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        state           TEXT NOT NULL,
        params          TEXT NOT NULL DEFAULT '{}',
        progress        INTEGER NOT NULL DEFAULT 0,
        step            TEXT NOT NULL DEFAULT '',
        error           TEXT,
        created_at      REAL NOT NULL,
        started_at      REAL,
        finished_at     REAL,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_jobs_state ON jobs (state, created_at);
    CREATE INDEX idx_jobs_kind_created ON jobs (kind, created_at DESC);

    -- Artifact bodies are files on the data volume; only their metadata is
    -- here. A collected log bundle is measured at 433 MB, which is not
    -- something to put in a database row, and using one path for both the
    -- small JSON written today and that bundle later means no second store
    -- has to be built.
    --
    -- ON DELETE CASCADE keeps the metadata honest when a job row goes; the
    -- files are removed by the same code path, which is the only thing that
    -- can do it.
    CREATE TABLE artifacts (
        id          TEXT PRIMARY KEY,
        job_id      TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        media_type  TEXT NOT NULL,
        size_bytes  INTEGER NOT NULL,
        sha256      TEXT NOT NULL,
        created_at  REAL NOT NULL
    );
    CREATE INDEX idx_artifacts_job ON artifacts (job_id);
    """,
    # 3 -> 4: which redaction rules are switched off.
    #
    # Only the exceptions are stored. A rule absent from this table is on, so a
    # rule added to app/redact.py in a later version is masked from the moment
    # it exists rather than needing a row written for it, and a database from an
    # older version has every new rule on by default. Storing the enabled set
    # instead would make a new rule silently inactive on every existing install
    # — the one failure mode that must not be possible here.
    #
    # The name is the rule's own identifier from RULES, not a foreign key: the
    # rules live in code. A row naming a rule that no longer exists is ignored
    # rather than being an error, which is what makes renaming one safe.
    """
    CREATE TABLE redaction_disabled (
        name        TEXT PRIMARY KEY,
        disabled_at REAL NOT NULL
    );
    """,
    # 4 -> 5: multiple users, roles, and the activity log.
    #
    # Before this, "who is logged in" was a single username/password-hash pair
    # read from the environment (XCP_PULSE_ADMIN_USER /
    # XCP_PULSE_ADMIN_PASSWORD_HASH) and sessions.username was never checked
    # against anything but that pair. Those two variables still work, but only
    # as the seed for the first admin row (see app/users.py) — the environment
    # is no longer where accounts live.
    #
    # role is checked in code (app/dependencies.py), not just here, but the
    # CHECK constraint stops a bad value ever reaching the table regardless of
    # which code path wrote it.
    #
    # sessions.username has no foreign key to this table (same reasoning as the
    # existing table: it is a bare string already). A disabled or deleted
    # user's old sessions are rejected by checking users at lookup time
    # (get_session_user), not by a constraint here.
    """
    CREATE TABLE users (
        id              TEXT PRIMARY KEY,
        username        TEXT NOT NULL UNIQUE,
        password_hash   TEXT NOT NULL,
        role            TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
        disabled        INTEGER NOT NULL DEFAULT 0,
        created_at      REAL NOT NULL
    );

    -- One row per action worth being able to answer "who did this, and when"
    -- about later: login/logout, settings changed, a job run, an artifact
    -- deleted, a user added or disabled. username is a bare string for the
    -- same reason sessions.username is: the record must survive the account
    -- being deleted later.
    CREATE TABLE activity_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT NOT NULL,
        action      TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
        ip          TEXT,
        created_at  REAL NOT NULL
    );
    CREATE INDEX idx_activity_log_created ON activity_log (created_at DESC);
    """,
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this app depends on.

    WAL keeps reads from blocking during the long writes that arrive with log
    collection; foreign_keys is off by default in SQLite and has to be asked for.
    """
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class _LockedCursor:
    """A ``sqlite3.Cursor`` wrapper that holds ``_LockedConnection``'s lock
    for every fetch, not just for the ``execute()`` call that produced it.

    A cursor steps the connection's own execution state on each fetch, the
    same as ``execute()`` does — so a fetch left unlocked can still
    interleave with another thread's statement on the shared connection and
    read back a corrupted ``sqlite3.Row``. See ``_LockedConnection`` for the
    crash this closes.
    """

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock) -> None:
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, *args, **kwargs):
        with self._lock:
            return self._cursor.fetchmany(*args, **kwargs)

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            row = self._cursor.fetchone()
        if row is None:
            raise StopIteration
        return row

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _LockedConnection:
    """A ``sqlite3.Connection`` wrapper that serialises every call with a lock.

    FastAPI runs synchronous route handlers in a thread pool, so a single
    request-serving connection (``app.state.db``) is called from whichever
    thread happens to be handling a given request — several at once, under
    load. ``check_same_thread=False`` only disables Python's same-thread
    assertion; it does not make SQLite's C-level connection object safe for
    concurrent statement execution from multiple threads, and two requests
    landing at the same instant can interleave cursor state on the shared
    connection. Reported as a real crash: ``/jobs`` returned a 500 with
    ``IndexError: tuple index out of range`` reading back a ``sqlite3.Row``
    that a concurrent query had corrupted mid-fetch, while the underlying
    data was intact — a second, uncontended read of the same rows worked.

    This wraps the connection itself (rather than requiring every call site to
    take a lock, which the whole codebase would have to remember to do) so
    ``app.state.db`` behaves exactly like a plain connection to every caller,
    with every method — ``execute``, ``executescript``, ``executemany``,
    ``commit``, ``rollback``, ``close`` and attribute access alike — going
    through one re-entrant lock. Re-entrant because a caller inside a
    ``with transaction(conn):`` block that then calls another function taking
    the same ``conn`` must not deadlock against itself on the same thread.

    ``execute()`` returns a ``_LockedCursor`` rather than the raw
    ``sqlite3.Cursor``, and that wrapper holds the same lock for every fetch.
    A ``sqlite3.Cursor`` is not a result set — it is a live handle into the
    connection's own execution state, and ``fetchall()``/``fetchone()`` step
    that shared state exactly as ``execute()`` does. Releasing the lock as
    soon as ``execute()`` returns left every fetch racing the *other*
    thread's ``execute()`` and fetches against the same underlying
    connection, which is what produced ``IndexError: tuple index out of
    range`` reading a ``sqlite3.Row`` mid-corruption — the lock was held for
    the one call that wasn't the race.

    Only the **web app's** connection is wrapped, via ``init_db`` below. The
    background job worker (``job_runner.JobWorker``) opens and uses its own
    connection on a single dedicated thread, so it was never part of this
    race and does not need the overhead of a lock it cannot contend.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, *args, **kwargs):
        with self._lock:
            cursor = self._conn.execute(*args, **kwargs)
            return _LockedCursor(cursor, self._lock)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            cursor = self._conn.executemany(*args, **kwargs)
            return _LockedCursor(cursor, self._lock)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __getattr__(self, name: str):
        # Anything not overridden above (row_factory reads, cursor(), etc.)
        # passes straight through to the real connection, unlocked — those
        # are either read-only attribute access or already-safe factory calls
        # that do not themselves touch shared cursor state.
        return getattr(self._conn, name)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a transaction, committing on success."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """Return the applied schema version, 0 for an untouched database."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration not yet applied. Returns the resulting version."""
    version = current_version(conn)
    for index, script in enumerate(_MIGRATIONS, start=1):
        if index <= version:
            continue
        with transaction(conn):
            conn.executescript(script)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (index,))
        version = index
    return version


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open the database and bring its schema up to date.

    Used for the web app's own connection (``app.state.db`` in ``main.py``),
    which is shared across whichever thread-pool thread happens to be serving
    a given request — so the connection this returns is wrapped in
    ``_LockedConnection`` to serialise concurrent access. See its docstring
    for the crash this fixes. The background job worker calls ``connect``
    directly instead, on its own single dedicated thread, and does not need
    the wrapper.
    """
    conn = connect(db_path)
    migrate(conn)
    return _LockedConnection(conn)
