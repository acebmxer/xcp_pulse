"""SQLite access and schema management.

One connection factory, one schema definition, one migration path. Later
stages add tables here (xo_connection for the XO link; jobs and artifacts for
collection) by appending a migration, never by editing an applied one.
"""

from __future__ import annotations

import sqlite3
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
    """Open the database and bring its schema up to date."""
    conn = connect(db_path)
    migrate(conn)
    return conn
