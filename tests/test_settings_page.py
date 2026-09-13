"""The Settings page: the Xen Orchestra connection form, and TLS certificate upload."""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import hash_password
from tests.conftest import TEST_PASSWORD, TEST_USER


def _make_pair(*, cn: str = "xcp-pulse-test") -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
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
    return cert_pem, key_pem


@pytest.fixture
def https_settings(tmp_path: Path) -> Settings:
    """Settings with built-in HTTPS on, so the TLS upload section is reachable.

    ``https_only`` (the Secure cookie flag) is deliberately left False here:
    TestClient talks to the app over plain HTTP regardless of enable_https,
    and a Secure cookie sent over plain HTTP is exactly what a real browser
    also discards — coupling the two in this fixture would fail every test
    below for the reason documented in installation.md's troubleshooting
    section, not for anything these tests are meant to check. The coupling
    itself (enable_https implying https_only) is covered in test_config.py.
    """
    return Settings(
        data_dir=tmp_path,
        admin_user=TEST_USER,
        admin_password_hash=hash_password(TEST_PASSWORD),
        secret_key="test-secret-key-not-for-production",
        https_only=False,
        enable_https=True,
        session_hours=12,
        login_max_attempts=5,
        login_lockout_minutes=15,
        log_level="WARNING",
    )


@pytest.fixture
def https_logged_in(https_settings: Settings) -> Iterator[TestClient]:
    app = create_app(https_settings)
    with TestClient(app, follow_redirects=False) as test_client:
        response = test_client.post(
            "/login",
            data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"},
        )
        assert response.status_code == 303, "fixture could not log in"
        yield test_client


def test_settings_page_renders(logged_in: TestClient) -> None:
    response = logged_in.get("/settings")
    assert response.status_code == 200
    assert "Xen Orchestra connection" in response.text


def test_tls_section_hidden_when_https_disabled(logged_in: TestClient) -> None:
    response = logged_in.get("/settings")
    assert "/settings/tls" not in response.text


def test_tls_section_shown_when_https_enabled(https_logged_in: TestClient) -> None:
    response = https_logged_in.get("/settings")
    assert "/settings/tls" in response.text


def test_tls_section_says_no_certificate_yet_before_one_exists(
    https_logged_in: TestClient,
) -> None:
    # Nothing has generated or uploaded a certificate in this fixture's
    # tmp_path yet, which is the real state right after enabling built-in
    # HTTPS before the entrypoint's openssl step has run.
    response = https_logged_in.get("/settings")
    assert "No certificate found yet" in response.text


def test_tls_section_reflects_the_uploaded_certificate_after_saving(
    https_logged_in: TestClient,
) -> None:
    # The page must describe the certificate actually in use, not a static
    # claim that was only ever true before the first upload.
    cert_pem, key_pem = _make_pair(cn="my-real-hostname.example.com")
    https_logged_in.post(
        "/settings/tls",
        files={
            "cert_file": ("cert.pem", cert_pem, "application/x-pem-file"),
            "key_file": ("key.pem", key_pem, "application/x-pem-file"),
        },
    )
    response = https_logged_in.get("/settings")
    assert "my-real-hostname.example.com" in response.text


def test_uploading_a_valid_pair_is_accepted(https_logged_in: TestClient, tmp_path: Path) -> None:
    cert_pem, key_pem = _make_pair()
    response = https_logged_in.post(
        "/settings/tls",
        files={
            "cert_file": ("cert.pem", cert_pem, "application/x-pem-file"),
            "key_file": ("key.pem", key_pem, "application/x-pem-file"),
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tls_saved=1"
    assert (tmp_path / "tls" / "cert.pem").read_bytes() == cert_pem
    assert (tmp_path / "tls" / "key.pem").read_bytes() == key_pem


def test_uploading_a_mismatched_pair_is_rejected(
    https_logged_in: TestClient, tmp_path: Path
) -> None:
    cert_pem, _key_pem = _make_pair()
    _other_cert, other_key_pem = _make_pair()
    response = https_logged_in.post(
        "/settings/tls",
        files={
            "cert_file": ("cert.pem", cert_pem, "application/x-pem-file"),
            "key_file": ("key.pem", other_key_pem, "application/x-pem-file"),
        },
    )
    assert response.status_code == 400
    assert "don&#39;t match" in response.text or "don't match" in response.text
    assert not (tmp_path / "tls" / "cert.pem").exists()


def test_uploading_garbage_is_rejected(https_logged_in: TestClient) -> None:
    response = https_logged_in.post(
        "/settings/tls",
        files={
            "cert_file": ("cert.pem", b"nonsense", "application/x-pem-file"),
            "key_file": ("key.pem", b"nonsense", "application/x-pem-file"),
        },
    )
    assert response.status_code == 400


def test_upload_route_refuses_when_https_disabled(logged_in: TestClient, tmp_path: Path) -> None:
    # The form is hidden in that case, but the route itself must not trust
    # that — a hidden control is not an access control.
    cert_pem, key_pem = _make_pair()
    response = logged_in.post(
        "/settings/tls",
        files={
            "cert_file": ("cert.pem", cert_pem, "application/x-pem-file"),
            "key_file": ("key.pem", key_pem, "application/x-pem-file"),
        },
    )
    assert response.status_code == 400
    assert "not enabled" in response.text


def test_upload_route_requires_login(client: TestClient) -> None:
    cert_pem, key_pem = _make_pair()
    response = client.post(
        "/settings/tls",
        files={
            "cert_file": ("cert.pem", cert_pem, "application/x-pem-file"),
            "key_file": ("key.pem", key_pem, "application/x-pem-file"),
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
