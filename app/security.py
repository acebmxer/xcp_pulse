"""Authentication: password hashing, sessions, and login throttling.

Sessions are stored server-side in SQLite and referenced by a signed cookie.
The signature stops a client forging a session id; the database row is what
makes logout genuinely invalidate a session rather than merely asking the
browser to forget it.
"""

from __future__ import annotations

import secrets
import sqlite3
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer

SESSION_COOKIE = "xcp_pulse_session"

# argon2-cffi's defaults are the RFC 9106 low-memory profile — appropriate here,
# where the cost is paid on a single interactive login rather than in bulk.
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return an Argon2id hash for storage in the environment."""
    return _hasher.hash(plain)


def verify_password(plain: str, stored_hash: str) -> bool:
    """Check a password against its hash, false on any mismatch or bad hash."""
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def _serializer(secret_key: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret_key, salt="xcp-pulse-session")


def sign_session_id(session_id: str, secret_key: str) -> str:
    """Wrap a session id in a signed cookie value."""
    return _serializer(secret_key).dumps(session_id)


def unsign_session_id(cookie_value: str, secret_key: str) -> str | None:
    """Recover a session id from a cookie value, None if the signature fails."""
    try:
        value = _serializer(secret_key).loads(cookie_value)
    except BadSignature:
        return None
    return value if isinstance(value, str) else None


def create_session(conn: sqlite3.Connection, username: str, session_hours: int) -> str:
    """Record a new session and return its id."""
    session_id = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, username, now, now + session_hours * 3600),
    )
    conn.commit()
    return session_id


def get_session_user(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Return the username for a live session, None if missing, expired, or
    the account behind it no longer exists or has been disabled.

    The account check is what makes disabling or deleting a user take effect
    immediately rather than only on their next login: sessions.username has no
    foreign key to users (same as before multiple accounts existed), so a
    disabled user's existing cookie would otherwise keep working until it
    expired on its own, up to XCP_PULSE_SESSION_HOURS later.
    """
    row = conn.execute(
        "SELECT username, expires_at FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < time.time():
        destroy_session(conn, session_id)
        return None

    user_row = conn.execute(
        "SELECT disabled FROM users WHERE username = ?", (row["username"],)
    ).fetchone()
    if user_row is None or user_row["disabled"]:
        destroy_session(conn, session_id)
        return None

    return row["username"]


def touch_session(conn: sqlite3.Connection, session_id: str, session_hours: int) -> None:
    """Extend a session's expiry — the sliding window on continued use."""
    conn.execute(
        "UPDATE sessions SET expires_at = ? WHERE id = ?",
        (time.time() + session_hours * 3600, session_id),
    )
    conn.commit()


def destroy_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Delete a session so its cookie stops working immediately."""
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    """Delete every expired session. Returns how many were removed."""
    cursor = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.commit()
    return cursor.rowcount


def client_ip(request: Request) -> str:
    """Best-effort client address for throttling.

    X-Forwarded-For is honoured because this is expected to sit behind a
    reverse proxy. It is spoofable when exposed directly, which is why the
    documentation says to run this behind a proxy on a trusted network.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def record_login_failure(conn: sqlite3.Connection, ip: str) -> None:
    """Record a failed login for rate-limiting purposes."""
    conn.execute("INSERT INTO login_attempts (ip, attempted_at) VALUES (?, ?)", (ip, time.time()))
    conn.commit()


def clear_login_failures(conn: sqlite3.Connection, ip: str) -> None:
    """Forget an address's failures after it succeeds."""
    conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
    conn.commit()


def login_failure_count(conn: sqlite3.Connection, ip: str, window_minutes: int) -> int:
    """Count an address's failures inside the lockout window."""
    cutoff = time.time() - window_minutes * 60
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND attempted_at > ?",
        (ip, cutoff),
    ).fetchone()
    return int(row["n"])


def is_rate_limited(
    conn: sqlite3.Connection, ip: str, max_attempts: int, window_minutes: int
) -> bool:
    """True when an address has failed too often to be allowed another try."""
    return login_failure_count(conn, ip, window_minutes) >= max_attempts


def purge_old_login_attempts(conn: sqlite3.Connection, window_minutes: int) -> int:
    """Drop login attempts older than the window. Returns how many were removed."""
    cutoff = time.time() - window_minutes * 60
    cursor = conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def current_user(request: Request) -> str | None:
    """Return the logged-in username for a request, or None.

    Reads the signed cookie, resolves it against the sessions table, and slides
    the expiry forward. Used by the login_required dependency and by templates
    that need to know who is looking.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None

    settings = request.app.state.settings
    session_id = unsign_session_id(cookie, settings.secret_key)
    if session_id is None:
        return None

    conn = request.app.state.db
    username = get_session_user(conn, session_id)
    if username is not None:
        touch_session(conn, session_id, settings.session_hours)
    return username
