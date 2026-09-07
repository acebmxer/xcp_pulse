"""Token encryption.

The token is the most sensitive thing XCP Pulse stores — on most Xen Orchestra
instances it is equivalent to pool admin credentials — so these assert the
properties that matter rather than merely that a round trip works.
"""

from __future__ import annotations

import pytest

from app.crypto import DecryptionError, decrypt, encrypt

KEY = "test-secret-key-not-for-production"
TOKEN = "_upHbv0ea_J--nz3K6feiPhOSTV2yl7JhBoeO6G4cRA"


def test_round_trip() -> None:
    assert decrypt(encrypt(TOKEN, KEY), KEY) == TOKEN


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    """The stored value must not leak the token to anyone reading the database."""
    stored = encrypt(TOKEN, KEY)
    assert TOKEN not in stored


def test_same_plaintext_encrypts_differently_each_time() -> None:
    """A fresh nonce per call, so identical tokens do not produce identical rows."""
    assert encrypt(TOKEN, KEY) != encrypt(TOKEN, KEY)


def test_wrong_key_is_rejected_rather_than_returning_garbage() -> None:
    """A changed secret key must fail loudly, not hand back an unusable token.

    Silently returning nonsense would surface much later as an unexplained
    authentication failure against Xen Orchestra.
    """
    stored = encrypt(TOKEN, KEY)
    with pytest.raises(DecryptionError):
        decrypt(stored, "a-different-secret-key")


def test_tampered_ciphertext_is_rejected() -> None:
    """AES-GCM authenticates the ciphertext, so alteration is detected here."""
    stored = encrypt(TOKEN, KEY)
    tampered = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
    with pytest.raises(DecryptionError):
        decrypt(tampered, KEY)


@pytest.mark.parametrize("value", ["", "not-base64!!", "c2hvcnQ="])
def test_malformed_values_raise_decryption_error(value: str) -> None:
    """Anything unreadable is a DecryptionError, never an unhandled exception."""
    with pytest.raises(DecryptionError):
        decrypt(value, KEY)


def test_unicode_survives_the_round_trip() -> None:
    assert decrypt(encrypt("tökén-λ-🔑", KEY), KEY) == "tökén-λ-🔑"
