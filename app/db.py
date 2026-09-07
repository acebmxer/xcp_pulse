"""SQLite access and schema management.

One connection factory, one schema definition, one migration path. Later
stages add tables here (xo_connection in v0.2.0, jobs and artifacts in
v0.4.0) by appending a migration, never by editing an applied one.
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
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this app depends on.

    WAL keeps reads from blocking during the long writes that arrive in
    v0.4.0; foreign_keys is off by default in SQLite and has to be asked for.
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
