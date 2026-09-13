"""Validating and installing an uploaded TLS certificate/key pair."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from app.tls import (
    CertificateError,
    current_certificate_info,
    install_certificate,
    reload_nginx,
    validate_certificate_pair,
)


def _make_pair(
    *, days: int = 365, cn: str = "xcp-pulse-test", key_size: int = 2048
) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return cert_pem, key_pem


def _make_ca_signed_pair(*, cn: str = "leaf.example.internal") -> tuple[bytes, bytes]:
    """A cert whose issuer differs from its subject — i.e. not self-signed."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365))
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return cert_pem, key_pem


def test_valid_matching_pair_is_accepted() -> None:
    cert_pem, key_pem = _make_pair(cn="xcp-pulse.example.internal")
    info = validate_certificate_pair(cert_pem, key_pem)
    assert "xcp-pulse.example.internal" in info.subject


def test_self_signed_pair_is_flagged_as_such() -> None:
    cert_pem, key_pem = _make_pair()
    info = validate_certificate_pair(cert_pem, key_pem)
    assert info.is_self_signed is True


def test_ca_signed_pair_is_not_flagged_self_signed() -> None:
    cert_pem, key_pem = _make_ca_signed_pair()
    info = validate_certificate_pair(cert_pem, key_pem)
    assert info.is_self_signed is False


def test_ec_key_is_accepted() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ec-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    validate_certificate_pair(cert_pem, key_pem)


def test_garbage_cert_is_rejected() -> None:
    _cert_pem, key_pem = _make_pair()
    with pytest.raises(CertificateError, match="PEM-encoded certificate"):
        validate_certificate_pair(b"not a certificate", key_pem)


def test_garbage_key_is_rejected() -> None:
    cert_pem, _key_pem = _make_pair()
    with pytest.raises(CertificateError, match="private key"):
        validate_certificate_pair(cert_pem, b"not a key")


def test_mismatched_cert_and_key_are_rejected() -> None:
    cert_pem, _key_pem = _make_pair()
    _other_cert_pem, other_key_pem = _make_pair()
    with pytest.raises(CertificateError, match="don't match"):
        validate_certificate_pair(cert_pem, other_key_pem)


def test_expired_certificate_is_rejected() -> None:
    cert_pem, key_pem = _make_pair(days=-1)
    with pytest.raises(CertificateError, match="expired"):
        validate_certificate_pair(cert_pem, key_pem)


def test_encrypted_key_is_rejected_with_a_clear_message() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encrypted_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"a-passphrase"),
    )
    cert_pem, _ = _make_pair()
    with pytest.raises(CertificateError, match="passphrase"):
        validate_certificate_pair(cert_pem, encrypted_key_pem)


def test_install_certificate_writes_both_files(tmp_path: Path) -> None:
    cert_pem, key_pem = _make_pair()
    tls_dir = tmp_path / "tls"
    install_certificate(tls_dir, cert_pem, key_pem)

    assert (tls_dir / "cert.pem").read_bytes() == cert_pem
    assert (tls_dir / "key.pem").read_bytes() == key_pem
    # No leftover temp files from the write-then-rename.
    assert not list(tls_dir.glob("*.tmp"))


def test_install_certificate_sets_restrictive_key_permissions(tmp_path: Path) -> None:
    cert_pem, key_pem = _make_pair()
    tls_dir = tmp_path / "tls"
    install_certificate(tls_dir, cert_pem, key_pem)

    mode = (tls_dir / "key.pem").stat().st_mode & 0o777
    assert mode == 0o600


def test_install_certificate_overwrites_an_existing_pair(tmp_path: Path) -> None:
    tls_dir = tmp_path / "tls"
    first_cert, first_key = _make_pair(cn="first")
    install_certificate(tls_dir, first_cert, first_key)

    second_cert, second_key = _make_pair(cn="second")
    install_certificate(tls_dir, second_cert, second_key)

    assert (tls_dir / "cert.pem").read_bytes() == second_cert
    assert (tls_dir / "key.pem").read_bytes() == second_key


def test_reload_nginx_without_a_pidfile_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # A normal state: uploading a certificate before HTTPS is even enabled,
    # so there is no nginx running yet to signal.
    from app import tls as tls_module

    monkeypatch.setattr(tls_module, "NGINX_PID_FILE", Path("/nonexistent/nginx.pid"))
    assert reload_nginx() is False


def test_current_certificate_info_reads_the_self_signed_one(tmp_path: Path) -> None:
    # docker/entrypoint.sh's self-signed cert, simulated here rather than
    # shelling out to openssl — the property under test is that this module
    # reads whatever is actually on disk, not how that file got there.
    cert_pem, key_pem = _make_pair(cn="xcp-pulse")
    tls_dir = tmp_path / "tls"
    install_certificate(tls_dir, cert_pem, key_pem)

    info = current_certificate_info(tls_dir)
    assert info is not None
    assert info.is_self_signed is True


def test_current_certificate_info_reflects_an_uploaded_replacement(tmp_path: Path) -> None:
    tls_dir = tmp_path / "tls"
    self_signed_cert, self_signed_key = _make_pair(cn="xcp-pulse")
    install_certificate(tls_dir, self_signed_cert, self_signed_key)
    assert current_certificate_info(tls_dir).is_self_signed is True

    # Replacing it with an uploaded, CA-signed pair — the whole point of the
    # upload feature — must be visible immediately in what this function
    # reports, not stuck describing the certificate that used to be there.
    uploaded_cert, uploaded_key = _make_ca_signed_pair(cn="xcp-pulse.example.com")
    install_certificate(tls_dir, uploaded_cert, uploaded_key)

    info = current_certificate_info(tls_dir)
    assert info.is_self_signed is False
    assert "xcp-pulse.example.com" in info.subject


def test_current_certificate_info_with_no_certificate_yet_returns_none(tmp_path: Path) -> None:
    # A normal state right after enabling built-in HTTPS, before the
    # entrypoint has generated anything yet.
    assert current_certificate_info(tmp_path / "tls") is None
