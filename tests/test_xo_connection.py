"""Storing the Xen Orchestra connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.crypto import DecryptionError
from app.db import init_db
from app.xo_connection import (
    build_client,
    delete_connection,
    get_connection,
    record_test_result,
    save_connection,
)

KEY = "test-secret-key-not-for-production"
TOKEN = "_upHbv0ea_J--nz3K6feiPhOSTV2yl7JhBoeO6G4cRA"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def _save(conn: sqlite3.Connection, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "url": "https://xo.example.com",
        "token": TOKEN,
        "account_type": "admin",
        "verify_tls": True,
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
    assert stored.url == "https://xo.example.com"
    assert stored.account_type == "admin"
    assert stored.verify_tls is True


def test_token_is_never_returned_by_get_connection(conn: sqlite3.Connection) -> None:
    """The settings page renders this object, so the token must not be on it."""
    _save(conn)
    stored = get_connection(conn)
    assert not any(TOKEN in str(value) for value in vars(stored).values())


def test_token_is_encrypted_in_the_database(conn: sqlite3.Connection) -> None:
    _save(conn)
    row = conn.execute("SELECT token_encrypted FROM xo_connection WHERE id = 1").fetchone()
    assert TOKEN not in row["token_encrypted"]


def test_trailing_slash_is_stripped_from_the_url(conn: sqlite3.Connection) -> None:
    """So a URL saved with a slash cannot produce '//rest/v0' when joined."""
    _save(conn, url="https://xo.example.com/")
    stored = get_connection(conn)
    assert stored is not None
    assert stored.url == "https://xo.example.com"


def test_saving_again_replaces_rather_than_adding(conn: sqlite3.Connection) -> None:
    """One connection at a time — the schema pins it to a single row."""
    _save(conn)
    _save(conn, url="https://other.example.com")
    count = conn.execute("SELECT COUNT(*) AS n FROM xo_connection").fetchone()["n"]
    assert count == 1
    stored = get_connection(conn)
    assert stored is not None
    assert stored.url == "https://other.example.com"


def test_saving_clears_a_previous_test_result(conn: sqlite3.Connection) -> None:
    """A result describing the old credentials must not read as current."""
    _save(conn)
    record_test_result(conn, ok=True, message="Connected")
    _save(conn, url="https://other.example.com")
    stored = get_connection(conn)
    assert stored is not None
    assert stored.last_test_ok is None
    assert stored.tested is False


def test_record_test_result(conn: sqlite3.Connection) -> None:
    _save(conn)
    record_test_result(conn, ok=False, message="token rejected")
    stored = get_connection(conn)
    assert stored is not None
    assert stored.last_test_ok is False
    assert stored.last_test_message == "token rejected"
    assert stored.tested is True


def test_delete_removes_the_token(conn: sqlite3.Connection) -> None:
    _save(conn)
    assert delete_connection(conn) is True
    assert get_connection(conn) is None
    count = conn.execute("SELECT COUNT(*) AS n FROM xo_connection").fetchone()["n"]
    assert count == 0


def test_rejects_an_unknown_account_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        _save(conn, account_type="superuser")


def test_build_client_without_a_connection(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        build_client(conn, KEY)


def test_build_client_decrypts_the_token(conn: sqlite3.Connection) -> None:
    _save(conn)
    client = build_client(conn, KEY)
    assert client.url == "https://xo.example.com"


def test_build_client_with_a_changed_secret_key(conn: sqlite3.Connection) -> None:
    """Changing the key invalidates the token, and must say so distinctly."""
    _save(conn)
    with pytest.raises(DecryptionError):
        build_client(conn, "a-different-secret-key")
