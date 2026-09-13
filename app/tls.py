"""The built-in HTTPS certificate: validating an upload, and telling nginx.

nginx (docker/entrypoint.sh) generates a self-signed certificate on first run
if none exists, and terminates TLS with whatever is at ``<data_dir>/tls/``.
This module is the other way a certificate gets there: an operator with their
own cert/key uploads them through Settings.

Only reachable when ``XCP_PULSE_ENABLE_HTTPS`` is on — without it there is no
nginx, no ``tls/`` directory convention worth writing to, and nothing to
signal.
"""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

log = logging.getLogger("xcp_pulse.tls")

# Written by docker/entrypoint.sh, read by nginx directly — the one place
# nginx's own pid is recorded, so this is also the one place to look for it.
NGINX_PID_FILE = Path("/tmp/nginx/run/nginx.pid")


class CertificateError(ValueError):
    """The uploaded cert/key is not usable. The message is shown to the user."""


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    not_valid_after: str
    is_self_signed: bool


def _parse_cert(pem_bytes: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem_bytes, default_backend())
    except ValueError as exc:
        raise CertificateError(
            "That doesn't look like a PEM-encoded certificate "
            "(it should start with -----BEGIN CERTIFICATE-----)."
        ) from exc


def _parse_key(pem_bytes: bytes) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )
    except (ValueError, TypeError) as exc:
        raise CertificateError(
            "That doesn't look like a PEM-encoded, unencrypted private key "
            "(it should start with -----BEGIN PRIVATE KEY----- or "
            "-----BEGIN RSA PRIVATE KEY-----). A key protected by a "
            "passphrase is not supported — nginx would have no way to be "
            "asked for it on every restart."
        ) from exc
    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise CertificateError("Only RSA and EC private keys are supported.")
    return key


def _public_numbers_match(
    cert: x509.Certificate, key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
) -> bool:
    """Whether ``key`` is the private half of ``cert``'s public key.

    Comparing public numbers rather than key bytes catches the single most
    likely mistake — pasting a cert and key that do not belong together —
    without needing to sign anything to prove it.
    """
    cert_public = cert.public_key()
    key_public = key.public_key()
    if isinstance(cert_public, rsa.RSAPublicKey) and isinstance(key_public, rsa.RSAPublicKey):
        return cert_public.public_numbers() == key_public.public_numbers()
    if isinstance(cert_public, ec.EllipticCurvePublicKey) and isinstance(
        key_public, ec.EllipticCurvePublicKey
    ):
        return cert_public.public_numbers() == key_public.public_numbers()
    return False


def validate_certificate_pair(cert_pem: bytes, key_pem: bytes) -> CertificateInfo:
    """Check that ``cert_pem`` and ``key_pem`` are a usable, matching pair.

    Raises ``CertificateError`` with a message safe to show the operator on
    the first problem found, rather than collecting every possible issue —
    the first one is usually why the rest fail too (a mismatched pair reads
    as a bad key just as easily as a bad cert).
    """
    cert = _parse_cert(cert_pem)
    key = _parse_key(key_pem)

    if not _public_numbers_match(cert, key):
        raise CertificateError(
            "The certificate and key don't match — this key cannot be used "
            "to serve that certificate."
        )

    now = datetime.now(UTC)
    not_after = cert.not_valid_after_utc
    if not_after < now:
        raise CertificateError(
            f"That certificate expired on {not_after:%Y-%m-%d} — it cannot be used."
        )

    return CertificateInfo(
        subject=cert.subject.rfc4514_string(),
        not_valid_after=not_after.strftime("%Y-%m-%d"),
        is_self_signed=cert.issuer == cert.subject,
    )


def current_certificate_info(tls_dir: Path) -> CertificateInfo | None:
    """What nginx is actually serving right now, read straight off disk.

    Settings needs to say whether the certificate in use is the self-signed
    one generated on first run or a replacement the operator uploaded — a
    static claim of "currently serving a self-signed certificate" goes wrong
    the moment an upload succeeds, so the page reads the real file instead of
    asserting a fact that was only ever true at one point in time.

    Returns ``None`` if there is nothing readable there yet (HTTPS enabled
    but the entrypoint hasn't generated one, or the file is unexpectedly
    missing) rather than raising — this is informational, not something that
    should break the Settings page.
    """
    cert_path = tls_dir / "cert.pem"
    if not cert_path.is_file():
        return None
    try:
        cert = _parse_cert(cert_path.read_bytes())
    except CertificateError:
        return None
    return CertificateInfo(
        subject=cert.subject.rfc4514_string(),
        not_valid_after=cert.not_valid_after_utc.strftime("%Y-%m-%d"),
        is_self_signed=cert.issuer == cert.subject,
    )


def install_certificate(tls_dir: Path, cert_pem: bytes, key_pem: bytes) -> None:
    """Write a validated cert/key pair to ``tls_dir`` and ask nginx to pick it up.

    Writes to temporary files first and renames into place, so a crash or a
    concurrent request never leaves nginx pointed at a half-written key —
    ``os.replace`` is atomic on the same filesystem, and ``tls_dir`` and its
    temp files are always on the data volume together.
    """
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_tmp = tls_dir / "cert.pem.tmp"
    key_tmp = tls_dir / "key.pem.tmp"

    cert_tmp.write_bytes(cert_pem)
    key_tmp.write_bytes(key_pem)
    key_tmp.chmod(0o600)

    os.replace(cert_tmp, tls_dir / "cert.pem")
    os.replace(key_tmp, tls_dir / "key.pem")

    reload_nginx()


def reload_nginx() -> bool:
    """Ask the running nginx to reload its config and re-read the certificate.

    SIGHUP is nginx's own documented reload signal: the master process
    re-reads its config and opens new listening sockets and files (including
    the certificate) before closing the old ones, so an in-flight request is
    never dropped for this. Returns whether a signal was actually sent —
    False (logged, not raised) when nginx isn't running, which is a normal
    state to upload a certificate in ahead of enabling HTTPS.
    """
    if not NGINX_PID_FILE.is_file():
        log.info("no nginx pidfile at %s — certificate saved, nginx not signalled", NGINX_PID_FILE)
        return False
    try:
        pid = int(NGINX_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGHUP)
    except (OSError, ValueError) as exc:
        log.warning("could not signal nginx to reload (pid file %s): %s", NGINX_PID_FILE, exc)
        return False
    log.info("nginx signalled to reload its certificate")
    return True
