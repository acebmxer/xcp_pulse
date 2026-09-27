"""Loading keys and host-key trust-on-first-use, shared by every SSH check."""

from __future__ import annotations

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.ssh_client import SshError, TrustOnFirstUseHostKeyPolicy, load_private_key


def _generate_key(passphrase: str | None = None) -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=encryption,
    ).decode("ascii")


def test_load_private_key_without_a_passphrase() -> None:
    pem = _generate_key()
    key = load_private_key(pem, None)
    assert isinstance(key, paramiko.PKey)


def test_load_private_key_with_a_correct_passphrase() -> None:
    pem = _generate_key("hunter2")
    key = load_private_key(pem, "hunter2")
    assert isinstance(key, paramiko.PKey)


def test_load_private_key_with_a_wrong_passphrase() -> None:
    pem = _generate_key("hunter2")
    with pytest.raises(SshError, match="passphrase"):
        load_private_key(pem, "wrong")


def test_load_private_key_that_needs_a_passphrase_but_none_given() -> None:
    pem = _generate_key("hunter2")
    with pytest.raises(SshError, match="passphrase"):
        load_private_key(pem, None)


def test_load_private_key_that_is_not_a_key_at_all() -> None:
    with pytest.raises(SshError):
        load_private_key("not a key", None)


def test_trust_on_first_use_records_an_unknown_host() -> None:
    pem = _generate_key()
    presented = load_private_key(pem, None)
    trusted: list[tuple[str, bytes]] = []
    policy = TrustOnFirstUseHostKeyPolicy(None, lambda t, b: trusted.append((t, b)))

    policy.missing_host_key(client=None, hostname="10.0.0.1", key=presented)

    assert policy.trusted_new_key is True
    assert trusted == [(presented.get_name(), presented.asbytes())]


def test_trust_on_first_use_accepts_a_matching_known_host() -> None:
    pem = _generate_key()
    presented = load_private_key(pem, None)
    known = (presented.get_name(), presented.asbytes())
    policy = TrustOnFirstUseHostKeyPolicy(known, lambda t, b: pytest.fail("must not be called"))

    policy.missing_host_key(client=None, hostname="10.0.0.1", key=presented)

    assert policy.trusted_new_key is False


def test_trust_on_first_use_rejects_a_changed_host_key() -> None:
    original = load_private_key(_generate_key(), None)
    changed = load_private_key(_generate_key(), None)
    known = (original.get_name(), original.asbytes())
    policy = TrustOnFirstUseHostKeyPolicy(known, lambda t, b: pytest.fail("must not be called"))

    with pytest.raises(SshError, match="different SSH host key"):
        policy.missing_host_key(client=None, hostname="10.0.0.1", key=changed)
