"""Storing and retrieving the SSH connections used to reach hosts directly.

XCP-ng officially deprecates dom0 user management: the only account it
supports for host-level SSH is root, the same one Xen Orchestra itself uses to
reach a host. There is no lesser account to create here. The privilege limit
instead comes from the host side: the matching public key must be added to
root's ``authorized_keys`` with a forced-command dispatcher script that
allowlists a fixed set of read-only checks by name and refuses anything else
— never a shell, never an arbitrary command. ``docs/configuration.md`` has the
exact setup.

**One connection per host, not one shared across every host.** A single
root-capable key reused everywhere means a compromised host's key also opens
every other host it was ever added to — this table keys each stored key by
the host it belongs to (``host_id``, the same id Xen Orchestra's inventory
assigns that host) so a key only ever reaches the one host it was set up for.
Every check that needs to reach a host over SSH (NIC statistics today, more
may follow) shares the *same stored key for that host*, but a second host
needs its own key saved separately — nothing here mixes the two.

The private key (and passphrase, if the key has one) is encrypted on the way
in and decrypted only when a connection to a host is about to be made, the
same way ``xo_connection`` handles the XO API token. Nothing here returns the
key to a template: the settings page shows whether one is stored, never its
value.
"""

from __future__ import annotations

import base64
import sqlite3
import time
from dataclasses import dataclass

from app.crypto import DecryptionError, decrypt, encrypt

DEFAULT_PORT = 22


@dataclass(frozen=True)
class SshConnection:
    """The stored connection for one host, without the key or passphrase."""

    host_id: str
    port: int
    has_passphrase: bool
    updated_at: float
    last_tested_at: float | None
    last_test_ok: bool | None
    last_test_message: str | None

    @property
    def tested(self) -> bool:
        return self.last_tested_at is not None


def save_connection(
    conn: sqlite3.Connection,
    *,
    host_id: str,
    private_key: str,
    passphrase: str,
    port: int,
    secret_key: str,
) -> None:
    """Create or replace the connection for ``host_id``, encrypting the key
    and passphrase.

    Replaces rather than updates so there is never a moment with a stale key
    from a previous save. Test results are cleared because they describe the
    previous credentials and would otherwise read as current.

    An empty ``passphrase`` stores NULL rather than an encrypted empty
    string, so ``get_connection`` can tell "no passphrase" apart from "a
    passphrase that happens to be empty" without decrypting anything.
    """
    if not host_id:
        raise ValueError("a host is required")
    if not private_key.strip():
        raise ValueError("a private key is required")

    now = time.time()
    created = now
    existing = conn.execute(
        "SELECT created_at FROM ssh_connection WHERE host_id = ?", (host_id,)
    ).fetchone()
    if existing is not None:
        created = existing["created_at"]

    conn.execute(
        """
        INSERT OR REPLACE INTO ssh_connection
            (host_id, private_key_encrypted, passphrase_encrypted, port,
             created_at, updated_at, last_tested_at, last_test_ok, last_test_message)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
        """,
        (
            host_id,
            encrypt(private_key, secret_key),
            encrypt(passphrase, secret_key) if passphrase else None,
            port,
            created,
            now,
        ),
    )
    conn.commit()


def _from_row(row: sqlite3.Row) -> SshConnection:
    return SshConnection(
        host_id=row["host_id"],
        port=row["port"],
        has_passphrase=row["passphrase_encrypted"] is not None,
        updated_at=row["updated_at"],
        last_tested_at=row["last_tested_at"],
        last_test_ok=None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
        last_test_message=row["last_test_message"],
    )


def get_connection(conn: sqlite3.Connection, host_id: str) -> SshConnection | None:
    """Return the stored connection for ``host_id``, or None if none is set."""
    row = conn.execute("SELECT * FROM ssh_connection WHERE host_id = ?", (host_id,)).fetchone()
    if row is None:
        return None
    return _from_row(row)


def list_connections(conn: sqlite3.Connection) -> list[SshConnection]:
    """Every stored connection, one per host that has a key configured."""
    rows = conn.execute("SELECT * FROM ssh_connection").fetchall()
    return [_from_row(row) for row in rows]


def delete_connection(conn: sqlite3.Connection, host_id: str) -> bool:
    """Remove the stored connection for ``host_id``. True if one was removed."""
    cursor = conn.execute("DELETE FROM ssh_connection WHERE host_id = ?", (host_id,))
    conn.commit()
    return cursor.rowcount > 0


def record_test_result(conn: sqlite3.Connection, host_id: str, *, ok: bool, message: str) -> None:
    """Remember the outcome of the last connection test for ``host_id``."""
    conn.execute(
        """
        UPDATE ssh_connection
           SET last_tested_at = ?, last_test_ok = ?, last_test_message = ?
         WHERE host_id = ?
        """,
        (time.time(), 1 if ok else 0, message, host_id),
    )
    conn.commit()


@dataclass(frozen=True)
class SshCredentials:
    """The decrypted material a connection attempt needs. Never stored, never logged."""

    private_key: str
    passphrase: str | None
    port: int


def load_credentials(conn: sqlite3.Connection, host_id: str, secret_key: str) -> SshCredentials:
    """Decrypt the stored key for ``host_id``, for immediate use against it.

    Raises LookupError when nothing is configured for this host and
    DecryptionError when the secret key no longer matches what was stored —
    the same two distinct failures, with the same distinct fixes, as
    ``xo_connection.build_client``.
    """
    row = conn.execute(
        "SELECT private_key_encrypted, passphrase_encrypted, port FROM ssh_connection"
        " WHERE host_id = ?",
        (host_id,),
    ).fetchone()
    if row is None:
        raise LookupError("no SSH connection is configured for this host")

    passphrase = None
    if row["passphrase_encrypted"] is not None:
        passphrase = decrypt(row["passphrase_encrypted"], secret_key)

    return SshCredentials(
        private_key=decrypt(row["private_key_encrypted"], secret_key),
        passphrase=passphrase,
        port=row["port"],
    )


def known_host_key(conn: sqlite3.Connection, host: str) -> tuple[str, bytes] | None:
    """The SSH host key recorded for ``host`` on an earlier connection, or None."""
    row = conn.execute(
        "SELECT key_type, key_base64 FROM ssh_known_hosts WHERE host = ?", (host,)
    ).fetchone()
    if row is None:
        return None
    return row["key_type"], base64.b64decode(row["key_base64"])


def remember_host_key(conn: sqlite3.Connection, host: str, key_type: str, key_bytes: bytes) -> None:
    """Record a host's key the first time it is trusted.

    Called only by ``TrustOnFirstUseHostKeyPolicy`` at the moment a host with
    no recorded key is trusted — never to overwrite an existing row, which is
    what would silently accept a changed key rather than raising.
    """
    conn.execute(
        """
        INSERT INTO ssh_known_hosts (host, key_type, key_base64, first_seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (host, key_type, base64.b64encode(key_bytes).decode("ascii"), time.time()),
    )
    conn.commit()


def forget_host_key(conn: sqlite3.Connection, host: str) -> bool:
    """Remove a recorded host key, so the next connection trusts whatever key
    the host presents as if it were being connected to for the first time.

    For the legitimate case a changed key usually means — the host was
    reinstalled or its key was rotated — after the operator has verified the
    new key by another channel. This does not verify anything itself.
    """
    cursor = conn.execute("DELETE FROM ssh_known_hosts WHERE host = ?", (host,))
    conn.commit()
    return cursor.rowcount > 0


__all__ = [
    "DEFAULT_PORT",
    "DecryptionError",
    "SshConnection",
    "SshCredentials",
    "delete_connection",
    "forget_host_key",
    "get_connection",
    "known_host_key",
    "list_connections",
    "load_credentials",
    "record_test_result",
    "remember_host_key",
    "save_connection",
]
