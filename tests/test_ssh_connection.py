"""Storing the shared SSH connection used to reach a host directly."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.crypto import DecryptionError
from app.db import init_db
from app.ssh_connection import (
    delete_connection,
    forget_host_key,
    get_connection,
    known_host_key,
    load_credentials,
    record_test_result,
    remember_host_key,
    save_connection,
)

KEY = "test-secret-key-not-for-production"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-material\n-----END OPENSSH PRIVATE KEY-----"
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def _save(conn: sqlite3.Connection, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "private_key": PRIVATE_KEY,
        "passphrase": "",
        "port": 22,
        "secret_key": KEY,
    }
    kwargs.update(overrides)
    save_connection(conn, **kwargs)  # type: ignore[arg-type]


def test_none_configured_initially(conn: sqlite3.Connection) -> None:
    assert get_connection(conn) is None


def test_save_and_read_back(conn: sqlite3.Connection) -> None:
    _save(conn)
    stored = get_connection(conn)
    assert stored is not None
    assert stored.port == 22
    assert stored.has_passphrase is False


def test_key_is_never_returned_by_get_connection(conn: sqlite3.Connection) -> None:
    """The settings page renders this object, so the key must not be on it."""
    _save(conn)
    stored = get_connection(conn)
    assert not any(PRIVATE_KEY in str(value) for value in vars(stored).values())


def test_key_is_encrypted_in_the_database(conn: sqlite3.Connection) -> None:
    _save(conn)
    row = conn.execute("SELECT private_key_encrypted FROM ssh_connection WHERE id = 1").fetchone()
    assert PRIVATE_KEY not in row["private_key_encrypted"]


def test_empty_passphrase_is_stored_as_no_passphrase(conn: sqlite3.Connection) -> None:
    _save(conn, passphrase="")
    row = conn.execute("SELECT passphrase_encrypted FROM ssh_connection WHERE id = 1").fetchone()
    assert row["passphrase_encrypted"] is None
    assert get_connection(conn).has_passphrase is False


def test_a_real_passphrase_is_encrypted_and_flagged(conn: sqlite3.Connection) -> None:
    _save(conn, passphrase="hunter2")
    row = conn.execute("SELECT passphrase_encrypted FROM ssh_connection WHERE id = 1").fetchone()
    assert row["passphrase_encrypted"] is not None
    assert "hunter2" not in row["passphrase_encrypted"]
    assert get_connection(conn).has_passphrase is True


def test_rejects_an_empty_private_key(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        _save(conn, private_key="   ")


def test_saving_again_replaces_rather_than_adding(conn: sqlite3.Connection) -> None:
    _save(conn)
    _save(conn, port=2222)
    count = conn.execute("SELECT COUNT(*) AS n FROM ssh_connection").fetchone()["n"]
    assert count == 1
    assert get_connection(conn).port == 2222


def test_saving_clears_a_previous_test_result(conn: sqlite3.Connection) -> None:
    _save(conn)
    record_test_result(conn, ok=True, message="Connected")
    _save(conn, port=2222)
    stored = get_connection(conn)
    assert stored.last_test_ok is None
    assert stored.tested is False


def test_record_test_result(conn: sqlite3.Connection) -> None:
    _save(conn)
    record_test_result(conn, ok=False, message="key rejected")
    stored = get_connection(conn)
    assert stored.last_test_ok is False
    assert stored.last_test_message == "key rejected"


def test_delete_removes_the_key(conn: sqlite3.Connection) -> None:
    _save(conn)
    assert delete_connection(conn) is True
    assert get_connection(conn) is None


def test_load_credentials_without_a_connection(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        load_credentials(conn, KEY)


def test_load_credentials_decrypts_the_key(conn: sqlite3.Connection) -> None:
    _save(conn, passphrase="hunter2")
    creds = load_credentials(conn, KEY)
    assert creds.private_key == PRIVATE_KEY
    assert creds.passphrase == "hunter2"


def test_load_credentials_with_no_passphrase_returns_none(conn: sqlite3.Connection) -> None:
    _save(conn, passphrase="")
    assert load_credentials(conn, KEY).passphrase is None


def test_load_credentials_with_a_changed_secret_key(conn: sqlite3.Connection) -> None:
    _save(conn)
    with pytest.raises(DecryptionError):
        load_credentials(conn, "a-different-secret-key")


def test_known_host_key_starts_empty(conn: sqlite3.Connection) -> None:
    assert known_host_key(conn, "10.0.0.1") is None


def test_remember_and_read_back_a_host_key(conn: sqlite3.Connection) -> None:
    remember_host_key(conn, "10.0.0.1", "ssh-ed25519", b"\x01\x02\x03")
    assert known_host_key(conn, "10.0.0.1") == ("ssh-ed25519", b"\x01\x02\x03")


def test_forget_host_key(conn: sqlite3.Connection) -> None:
    remember_host_key(conn, "10.0.0.1", "ssh-ed25519", b"\x01\x02\x03")
    assert forget_host_key(conn, "10.0.0.1") is True
    assert known_host_key(conn, "10.0.0.1") is None


def test_forget_an_unknown_host_key(conn: sqlite3.Connection) -> None:
    assert forget_host_key(conn, "10.0.0.1") is False
