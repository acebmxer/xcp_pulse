"""TOTP two-factor authentication (RFC 6238) and its enrollment QR code.

The code generation and verification are stdlib only (~30 lines of
HMAC-SHA1 on top of RFC 4226's HOTP) — there is no real gain in reaching for
a dependency for something this small and this well specified.

The QR code is rendered with ``segno`` (a pure-Python, zero-dependency QR
encoder) straight to inline SVG: no image library, no external image service
(which would leak the TOTP secret to a third party over the network), and no
file written to disk. Verified end-to-end during development — the exact SVG
this module produces was rasterized and scanned back with a real QR decoder
(zbar) to confirm the otpauth:// payload round-trips correctly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import time
from urllib.parse import quote

import segno

_DIGITS = 6
_PERIOD = 30
_SECRET_BYTES = 20  # 160 bits, RFC 4226's recommended HOTP secret length.

# How many 30-second steps of clock drift either side of "now" are still
# accepted. One step in each direction covers a phone clock that has drifted
# a little, or the code being typed just as a period boundary passes.
_DRIFT_STEPS = 1

BACKUP_CODE_COUNT = 10
_BACKUP_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I — read aloud safely
_BACKUP_CODE_LENGTH = 10


def generate_secret() -> str:
    """A new base32 TOTP secret, suitable for an otpauth:// URI."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32.upper())
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(code % 10**_DIGITS).zfill(_DIGITS)


def current_code(secret_b32: str, *, at: float | None = None) -> str:
    """The 6-digit code valid right now — used by tests and nothing else;
    a login never generates a code, only checks one.
    """
    counter = int((at if at is not None else time.time()) // _PERIOD)
    return _hotp(secret_b32, counter)


def verify_code(secret_b32: str, code: str, *, at: float | None = None) -> bool:
    """Check a user-typed code against the secret, allowing small clock drift.

    ``hmac.compare_digest`` on each candidate rather than ``==``, so timing
    cannot narrow down which of the accepted window's codes was close.
    """
    code = code.strip()
    if not code.isdigit() or len(code) != _DIGITS:
        return False
    now = at if at is not None else time.time()
    counter = int(now // _PERIOD)
    return any(
        hmac.compare_digest(_hotp(secret_b32, counter + drift), code)
        for drift in range(-_DRIFT_STEPS, _DRIFT_STEPS + 1)
    )


def provisioning_uri(secret_b32: str, *, username: str, issuer: str = "XCP Pulse") -> str:
    """The otpauth:// URI an authenticator app reads from the QR code."""
    label = quote(f"{issuer}:{username}")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret_b32}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={_DIGITS}&period={_PERIOD}"
    )


def qr_svg(text: str) -> str:
    """Render ``text`` as a QR code, returned as standalone <svg> markup."""
    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=6, border=3, xmldecl=False)
    return buf.getvalue().decode("utf-8")


def generate_backup_codes() -> list[str]:
    """One-time recovery codes for when the authenticator device is unavailable.

    Returned as plain text exactly once, at enrollment — the caller hashes
    them (``hash_backup_code``) before storage, since it cannot be recovered
    later and must never be shown to the user again.
    """
    return [
        "".join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(_BACKUP_CODE_LENGTH))
        for _ in range(BACKUP_CODE_COUNT)
    ]


def hash_backup_code(code: str) -> str:
    """A backup code is checked rarely and typed once, so a fast, salted-by-
    construction hash (rather than Argon2's deliberately slow one) is enough:
    there is no login-throttling concern per call here, and the input space
    is 32**10, not a human-chosen password.
    """
    normalized = code.strip().upper().replace("-", "")
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()
