"""Storing and retrieving the Xen Orchestra connection.

The token is encrypted on the way in and decrypted only when a call to XO is
about to be made. Nothing here returns the token to a template: the settings
page shows whether a token is stored, never its value.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from app.crypto import DecryptionError, decrypt, encrypt
from app.xo_client import XoClient

# What the operator tells us the XO account is. XCP Pulse verifies this against
# the instance rather than trusting it, but recording what was intended lets
# the settings page point out a mismatch — an account believed to be restricted
# that turns out to be an administrator is worth saying out loud.
ACCOUNT_TYPES = ("admin", "restricted")


@dataclass(frozen=True)
class XoConnection:
    """The stored connection, without the token."""

    url: str
    account_type: str
    verify_tls: bool
    updated_at: float
    last_tested_at: float | None
    last_test_ok: bool | None
    last_test_message: str | None

    @property
    def tested(self) -> bool:
        return self.last_tested_at is not None

    @property
    def is_admin(self) -> bool:
        return self.account_type == "admin"


def save_connection(
    conn: sqlite3.Connection,
    *,
    url: str,
    token: str,
    account_type: str,
    verify_tls: bool,
    secret_key: str,
) -> None:
    """Create or replace the connection, encrypting the token.

    Replaces rather than updates so there is never a moment with a new URL and
    a stale token. Test results are cleared because they describe the previous
    credentials and would otherwise read as current.
    """
    if account_type not in ACCOUNT_TYPES:
        raise ValueError(f"account_type must be one of {ACCOUNT_TYPES}, got {account_type!r}")

    now = time.time()
    created = now
    existing = conn.execute("SELECT created_at FROM xo_connection WHERE id = 1").fetchone()
    if existing is not None:
        created = existing["created_at"]

    conn.execute(
        """
        INSERT OR REPLACE INTO xo_connection
            (id, url, token_encrypted, account_type, verify_tls,
             created_at, updated_at, last_tested_at, last_test_ok, last_test_message)
        VALUES (1, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
        """,
        (
            url.rstrip("/"),
            encrypt(token, secret_key),
            account_type,
            1 if verify_tls else 0,
            created,
            now,
        ),
    )
    conn.commit()


def get_connection(conn: sqlite3.Connection) -> XoConnection | None:
    """Return the stored connection, or None when none is configured."""
    row = conn.execute("SELECT * FROM xo_connection WHERE id = 1").fetchone()
    if row is None:
        return None
    return XoConnection(
        url=row["url"],
        account_type=row["account_type"],
        verify_tls=bool(row["verify_tls"]),
        updated_at=row["updated_at"],
        last_tested_at=row["last_tested_at"],
        last_test_ok=None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
        last_test_message=row["last_test_message"],
    )


def delete_connection(conn: sqlite3.Connection) -> bool:
    """Remove the stored connection and its token. True if one was removed."""
    cursor = conn.execute("DELETE FROM xo_connection WHERE id = 1")
    conn.commit()
    return cursor.rowcount > 0


def record_test_result(conn: sqlite3.Connection, *, ok: bool, message: str) -> None:
    """Remember the outcome of the last connection test."""
    conn.execute(
        """
        UPDATE xo_connection
           SET last_tested_at = ?, last_test_ok = ?, last_test_message = ?
         WHERE id = 1
        """,
        (time.time(), 1 if ok else 0, message),
    )
    conn.commit()


def build_client(conn: sqlite3.Connection, secret_key: str) -> XoClient:
    """Build a client for the stored connection.

    Raises LookupError when nothing is configured and DecryptionError when the
    secret key no longer matches the stored token — distinct failures with
    distinct fixes: configure a connection, or enter the token again.
    """
    row = conn.execute(
        "SELECT url, token_encrypted, verify_tls FROM xo_connection WHERE id = 1"
    ).fetchone()
    if row is None:
        raise LookupError("no Xen Orchestra connection is configured")

    token = decrypt(row["token_encrypted"], secret_key)
    return XoClient(row["url"], token, verify_tls=bool(row["verify_tls"]))


__all__ = [
    "ACCOUNT_TYPES",
    "DecryptionError",
    "XoConnection",
    "build_client",
    "delete_connection",
    "get_connection",
    "record_test_result",
    "save_connection",
]
