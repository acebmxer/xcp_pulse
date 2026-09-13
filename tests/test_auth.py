"""Login, logout, sessions and throttling."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import PENDING_2FA_COOKIE, SESSION_COOKIE
from app.totp import current_code
from app.users import confirm_totp_enrollment, get_user
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
        enable_https=False,
        session_hours=12,
        login_max_attempts=3,
        login_lockout_minutes=15,
        log_level="WARNING",
        enable_self_update=settings.enable_self_update,
        compose_project_dir=settings.compose_project_dir,
        is_dev_build=settings.is_dev_build,
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


# --- Two-factor login -------------------------------------------------------


def _enable_totp(client: TestClient, user_id: str) -> str:
    """Enroll TOTP for a user directly through the model layer and return the
    secret — the enrollment HTTP flow itself is covered in test_users.py.
    """
    from app.users import begin_totp_enrollment

    conn = client.app.state.db
    secret_key = client.app.state.settings.secret_key
    secret = begin_totp_enrollment(conn, user_id, secret_key)
    code = current_code(secret)
    backup_codes = confirm_totp_enrollment(conn, user_id, code, secret_key)
    assert backup_codes is not None, "fixture could not enroll TOTP"
    return secret


def test_correct_password_with_totp_on_does_not_create_a_session(client: TestClient) -> None:
    user = get_user(client.app.state.db, TEST_USER)
    assert user is not None
    _enable_totp(client, user.id)

    response = client.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login/2fa?next=/"
    assert not client.cookies.get(SESSION_COOKIE)
    assert client.cookies.get(PENDING_2FA_COOKIE)


def test_correct_totp_code_completes_login(client: TestClient) -> None:
    user = get_user(client.app.state.db, TEST_USER)
    assert user is not None
    secret = _enable_totp(client, user.id)

    client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
    response = client.post("/login/2fa", data={"code": current_code(secret), "next": "/"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get(SESSION_COOKIE)


def test_wrong_totp_code_is_rejected(client: TestClient) -> None:
    user = get_user(client.app.state.db, TEST_USER)
    assert user is not None
    _enable_totp(client, user.id)

    client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
    response = client.post("/login/2fa", data={"code": "000000", "next": "/"})
    assert response.status_code == 401
    assert not client.cookies.get(SESSION_COOKIE)


def test_backup_code_completes_login_and_is_consumed(client: TestClient) -> None:
    user = get_user(client.app.state.db, TEST_USER)
    assert user is not None
    secret_key = client.app.state.settings.secret_key
    from app.users import begin_totp_enrollment

    conn = client.app.state.db
    secret = begin_totp_enrollment(conn, user.id, secret_key)
    backup_codes = confirm_totp_enrollment(conn, user.id, current_code(secret), secret_key)
    assert backup_codes is not None
    one_time_code = backup_codes[0]

    client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
    response = client.post("/login/2fa", data={"code": one_time_code, "next": "/"})
    assert response.status_code == 303
    assert client.cookies.get(SESSION_COOKIE)

    # The same backup code must not work a second time.
    client.cookies.delete(SESSION_COOKIE)
    client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
    replay = client.post("/login/2fa", data={"code": one_time_code, "next": "/"})
    assert replay.status_code == 401


def test_2fa_step_is_unreachable_without_a_completed_password_step(client: TestClient) -> None:
    response = client.get("/login/2fa")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_account_without_totp_skips_straight_to_a_session(client: TestClient) -> None:
    # No enrollment in this test — the existing plain-login behaviour, kept as
    # a regression guard now that login_submit branches on totp_enabled.
    response = client.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get(SESSION_COOKIE)
