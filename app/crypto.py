"""Encryption for secrets held in the database.

Only the Xen Orchestra API token needs this today. It is stored encrypted
because the database sits on a volume that gets backed up and copied around,
and the token is powerful — on most instances it is equivalent to pool admin
credentials.

The key is derived from the application secret key rather than stored beside
the ciphertext, so a copy of the database alone does not decrypt. That ties the
token's lifetime to the secret key: change the key and the stored token can no
longer be read, which is why config documents that as a consequence.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Distinguishes this use of the secret key from cookie signing, so the same
# input key material cannot produce related keys across the two uses.
_KEY_INFO = b"xcp-pulse-token-encryption-v1"
_NONCE_BYTES = 12


class DecryptionError(RuntimeError):
    """Ciphertext could not be decrypted with this key.

    Raised for a corrupted value and for a changed secret key alike — they are
    indistinguishable, and both mean the token has to be entered again.
    """


def _derive_key(secret_key: str) -> bytes:
    """Turn the application secret key into a 256-bit AES key."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO)
    return hkdf.derive(secret_key.encode("utf-8"))


def encrypt(plaintext: str, secret_key: str) -> str:
    """Encrypt a secret for storage, returning base64 text.

    A fresh random nonce is generated per call and prepended to the ciphertext,
    so encrypting the same token twice produces different output and nothing
    can be inferred by comparing rows.
    """
    key = _derive_key(secret_key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(stored: str, secret_key: str) -> str:
    """Recover a secret encrypted by ``encrypt``.

    Raises DecryptionError rather than returning a partial or garbage value:
    AES-GCM authenticates the ciphertext, so tampering is detected here rather
    than surfacing later as a mysterious authentication failure against XO.
    """
    try:
        raw = base64.b64decode(stored, validate=True)
    except (ValueError, TypeError) as exc:
        raise DecryptionError("stored value is not valid base64") from exc

    if len(raw) <= _NONCE_BYTES:
        raise DecryptionError("stored value is too short to contain a nonce")

    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        plaintext = AESGCM(_derive_key(secret_key)).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError(
            "cannot decrypt with the current secret key — the key changed or the value was altered"
        ) from exc
    return plaintext.decode("utf-8")
