"""Login, logout, sessions and throttling."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import SESSION_COOKIE
from tests.conftest import TEST_PASSWORD, TEST_USER


def test_login_page_renders(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "XCP Pulse" in response.text


def test_correct_credentials_are_accepted(client: TestClient) -> None:
    response = client.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get(SESSION_COOKIE)


def test_wrong_password_is_rejected(client: TestClient) -> None:
    response = client.post("/login", data={"username": TEST_USER, "password": "wrong", "next": "/"})
    assert response.status_code == 401
    assert "Incorrect username or password" in response.text
    assert not client.cookies.get(SESSION_COOKIE)


def test_wrong_username_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/login", data={"username": "someone", "password": TEST_PASSWORD, "next": "/"}
    )
    assert response.status_code == 401
    assert not client.cookies.get(SESSION_COOKIE)


def test_session_persists_across_requests(logged_in: TestClient) -> None:
    for _ in range(3):
        assert logged_in.get("/").status_code == 200


def test_logout_invalidates_the_session(logged_in: TestClient) -> None:
    stolen = logged_in.cookies.get(SESSION_COOKIE)

    assert logged_in.post("/logout").status_code == 303
    assert logged_in.get("/").status_code == 303

    # The session is gone server-side, so replaying the old cookie fails too.
    logged_in.cookies.set(SESSION_COOKIE, stolen)
    assert logged_in.get("/").status_code == 303


def test_forged_cookie_is_rejected(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-signed-value")
    assert client.get("/").status_code == 303


def test_login_page_redirects_when_already_signed_in(logged_in: TestClient) -> None:
    response = logged_in.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_rate_limit_blocks_after_repeated_failures(settings: Settings) -> None:
    limited = settings.__class__(
        data_dir=settings.data_dir,
        admin_user=settings.admin_user,
        admin_password_hash=settings.admin_password_hash,
        secret_key=settings.secret_key,
        https_only=False,
        session_hours=12,
        login_max_attempts=3,
        login_lockout_minutes=15,
        log_level="WARNING",
    )
    with TestClient(create_app(limited), follow_redirects=False) as client:
        for _ in range(3):
            assert (
                client.post(
                    "/login", data={"username": TEST_USER, "password": "wrong", "next": "/"}
                ).status_code
                == 401
            )

        # Correct credentials now too — being locked out must not be bypassable
        # by simply guessing right on the next attempt.
        blocked = client.post(
            "/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"}
        )
        assert blocked.status_code == 429
        assert "Too many failed attempts" in blocked.text


def test_open_redirect_is_refused(client: TestClient) -> None:
    # A ?next= pointing off-site would turn the login page into an open redirect.
    for hostile in ("https://evil.example.com/", "//evil.example.com/"):
        client.cookies.clear()
        response = client.post(
            "/login",
            data={"username": TEST_USER, "password": TEST_PASSWORD, "next": hostile},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
