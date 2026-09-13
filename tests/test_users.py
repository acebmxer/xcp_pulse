"""Multiple users, roles, and the permission tiers they gate.

The bootstrapped admin (TEST_USER / TEST_PASSWORD, from conftest) always
exists once the app starts — these tests add operator and viewer accounts
alongside it and check what each can and cannot reach.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.users import MIN_PASSWORD_LENGTH, ROLES, UserError, create_user, get_user
from tests.conftest import TEST_USER


def _login_as(client: TestClient, username: str, password: str) -> TestClient:
    response = client.post("/login", data={"username": username, "password": password, "next": "/"})
    assert response.status_code == 303, f"could not log in as {username}"
    return client


def _add_user(client: TestClient, username: str, password: str, role: str) -> None:
    conn = client.app.state.db
    create_user(conn, username, password, role)


def test_bootstrap_creates_exactly_one_admin(client: TestClient) -> None:
    conn = client.app.state.db
    admin = get_user(conn, TEST_USER)
    assert admin is not None
    assert admin.role == "admin"
    assert not admin.disabled


def test_bootstrap_does_not_run_twice(client: TestClient) -> None:
    # create_app() -> lifespan already ran bootstrap_admin once for `client`.
    # A second pass over an already-seeded table must be a no-op, not a second
    # row or an error on the UNIQUE constraint.
    from app.users import bootstrap_admin

    conn = client.app.state.db
    bootstrap_admin(conn, "someone-else", "irrelevant-hash")
    assert get_user(conn, "someone-else") is None
    assert get_user(conn, TEST_USER) is not None


def test_viewer_can_see_pages_but_not_act(client: TestClient) -> None:
    _add_user(client, "viewer1", "viewer-password", "viewer")
    _login_as(client, "viewer1", "viewer-password")

    assert client.get("/jobs").status_code == 200
    assert client.get("/collect").status_code == 200
    assert client.get("/findings").status_code == 200

    # The activity log is operator and admin only.
    assert client.get("/activity").status_code == 403

    assert client.post("/jobs/refresh-inventory").status_code == 403
    assert client.post("/collect", data={"host_id": "x"}).status_code == 403
    assert client.post("/redaction/rules").status_code == 403
    assert client.get("/settings").status_code == 403
    assert client.get("/settings/users").status_code == 403


def test_operator_can_act_but_not_touch_settings(client: TestClient) -> None:
    _add_user(client, "operator1", "operator-password", "operator")
    _login_as(client, "operator1", "operator-password")

    assert client.get("/activity").status_code == 200
    assert client.post("/jobs/refresh-inventory").status_code in (303,)
    assert client.get("/settings").status_code == 403
    assert client.post("/redaction/rules").status_code == 403
    assert client.get("/settings/users").status_code == 403


def test_admin_can_reach_everything(logged_in: TestClient) -> None:
    assert logged_in.get("/settings").status_code == 200
    assert logged_in.get("/settings/users").status_code == 200
    assert logged_in.get("/activity").status_code == 200


def test_disabled_user_is_logged_out_immediately(client: TestClient) -> None:
    _add_user(client, "operator2", "operator-password", "operator")
    _login_as(client, "operator2", "operator-password")
    assert client.get("/jobs").status_code == 200

    from app.users import get_user, set_disabled

    conn = client.app.state.db
    user = get_user(conn, "operator2")
    assert user is not None
    set_disabled(conn, user.id, True)

    # The existing session cookie must stop working the moment the account is
    # disabled, not merely on its next expiry — see get_session_user.
    assert client.get("/jobs").status_code == 303


def test_disabled_user_cannot_log_in_and_is_told_why(client: TestClient) -> None:
    _add_user(client, "operator3", "operator-password", "operator")
    from app.users import get_user, set_disabled

    conn = client.app.state.db
    user = get_user(conn, "operator3")
    assert user is not None
    set_disabled(conn, user.id, True)

    response = client.post(
        "/login", data={"username": "operator3", "password": "operator-password", "next": "/"}
    )
    assert response.status_code == 401
    assert "disabled" in response.text.lower()


def test_wrong_password_on_a_disabled_account_still_gets_the_generic_message(
    client: TestClient,
) -> None:
    """Only someone who already knows the correct password should learn an
    account is disabled — a wrong password must fail exactly like a wrong
    password against any other account, revealing nothing.
    """
    _add_user(client, "operator4", "operator-password", "operator")
    from app.users import get_user, set_disabled

    conn = client.app.state.db
    user = get_user(conn, "operator4")
    assert user is not None
    set_disabled(conn, user.id, True)

    response = client.post(
        "/login", data={"username": "operator4", "password": "wrong-password", "next": "/"}
    )
    assert response.status_code == 401
    assert "disabled" not in response.text.lower()
    assert "Incorrect username or password" in response.text


def test_cannot_disable_the_last_admin(client: TestClient) -> None:
    from app.users import get_user, set_disabled

    conn = client.app.state.db
    admin = get_user(conn, TEST_USER)
    assert admin is not None
    with pytest.raises(UserError):
        set_disabled(conn, admin.id, True)


def test_cannot_demote_the_last_admin(client: TestClient) -> None:
    from app.users import get_user, set_role

    conn = client.app.state.db
    admin = get_user(conn, TEST_USER)
    assert admin is not None
    with pytest.raises(UserError):
        set_role(conn, admin.id, "operator")


def test_second_admin_allows_disabling_the_first(client: TestClient) -> None:
    _add_user(client, "admin2", "admin2-password", "admin")
    from app.users import get_user, set_disabled

    conn = client.app.state.db
    original = get_user(conn, TEST_USER)
    assert original is not None
    set_disabled(conn, original.id, True)  # must not raise now that a second admin exists
    assert get_user(conn, TEST_USER).disabled  # type: ignore[union-attr]


def test_duplicate_username_is_rejected(client: TestClient) -> None:
    conn = client.app.state.db
    with pytest.raises(UserError):
        create_user(conn, TEST_USER, "whatever12", "viewer")


def test_short_password_is_rejected_on_create(client: TestClient) -> None:
    conn = client.app.state.db
    with pytest.raises(UserError):
        create_user(conn, "shortpw", "a" * (MIN_PASSWORD_LENGTH - 1), "viewer")


def test_bad_role_is_rejected_on_create(client: TestClient) -> None:
    conn = client.app.state.db
    with pytest.raises(UserError):
        create_user(conn, "badrole", "a-real-password", "superuser")


def test_admin_can_reset_someone_elses_password_without_their_current_one(
    logged_in: TestClient,
) -> None:
    conn = logged_in.app.state.db
    create_user(conn, "resettable", "old-password-123", "viewer")
    from app.users import get_user

    target = get_user(conn, "resettable")
    assert target is not None

    response = logged_in.post(
        f"/settings/users/{target.id}/reset-password",
        data={"new_password": "new-password-456", "confirm_password": "new-password-456"},
    )
    assert response.status_code == 303

    # The old password no longer works, the new one does.
    fresh = TestClient(logged_in.app, follow_redirects=False)
    assert (
        fresh.post(
            "/login",
            data={"username": "resettable", "password": "old-password-123", "next": "/"},
        ).status_code
        == 401
    )
    assert (
        fresh.post(
            "/login",
            data={"username": "resettable", "password": "new-password-456", "next": "/"},
        ).status_code
        == 303
    )


def test_any_role_can_change_their_own_password(client: TestClient) -> None:
    _add_user(client, "selfchanger", "original-password", "viewer")
    _login_as(client, "selfchanger", "original-password")

    response = client.post(
        "/account/password",
        data={
            "current_password": "original-password",
            "new_password": "brand-new-password",
            "confirm_password": "brand-new-password",
        },
    )
    assert response.status_code == 303

    fresh = TestClient(client.app, follow_redirects=False)
    assert (
        fresh.post(
            "/login",
            data={
                "username": "selfchanger",
                "password": "brand-new-password",
                "next": "/",
            },
        ).status_code
        == 303
    )


def test_own_password_change_requires_the_current_password(client: TestClient) -> None:
    _add_user(client, "selfchanger2", "original-password", "viewer")
    _login_as(client, "selfchanger2", "original-password")

    response = client.post(
        "/account/password",
        data={
            "current_password": "wrong-password",
            "new_password": "brand-new-password",
            "confirm_password": "brand-new-password",
        },
    )
    assert response.status_code == 401


def test_viewer_cannot_reset_someone_elses_password(client: TestClient) -> None:
    _add_user(client, "viewer2", "viewer-password", "viewer")
    _add_user(client, "target-user", "target-password", "viewer")
    _login_as(client, "viewer2", "viewer-password")

    from app.users import get_user

    target = get_user(client.app.state.db, "target-user")
    assert target is not None

    response = client.post(
        f"/settings/users/{target.id}/reset-password",
        data={"new_password": "hijacked-pw-123", "confirm_password": "hijacked-pw-123"},
    )
    assert response.status_code == 403


def test_all_three_roles_are_offered_on_the_users_page(logged_in: TestClient) -> None:
    body = logged_in.get("/settings/users").text
    for role in ROLES:
        assert role in body


def test_viewer_can_download_but_not_start_or_delete(client: TestClient) -> None:
    """A viewer may read what is already stored, just not produce or remove
    it — per Nick's call that read-only should include downloads, not just
    page views.
    """
    from app.artifacts import store_json
    from app.jobs import enqueue

    app = client.app
    conn = app.state.db
    data_dir = app.state.settings.data_dir

    job = enqueue(conn, "collect_logs", {})
    artifact = store_json(conn, data_dir, job_id=job.id, name="example.json", payload={"ok": True})

    _add_user(client, "viewer3", "viewer-password", "viewer")
    _login_as(client, "viewer3", "viewer-password")

    download = client.get(f"/collect/download/{artifact.id}")
    assert download.status_code == 200

    assert client.post(f"/collect/{job.id}/delete").status_code == 403


def test_mismatched_confirmation_is_rejected_on_add_user(logged_in: TestClient) -> None:
    response = logged_in.post(
        "/settings/users",
        data={
            "new_username": "mismatched",
            "password": "one-password",
            "confirm_password": "a-different-password",
            "role": "viewer",
        },
    )
    assert response.status_code == 200
    assert "do not match" in response.text
    assert get_user(logged_in.app.state.db, "mismatched") is None


def test_mismatched_confirmation_is_rejected_on_admin_reset(logged_in: TestClient) -> None:
    conn = logged_in.app.state.db
    create_user(conn, "resetme", "old-password-123", "viewer")
    target = get_user(conn, "resetme")
    assert target is not None

    response = logged_in.post(
        f"/settings/users/{target.id}/reset-password",
        data={"new_password": "new-password-1", "confirm_password": "new-password-2"},
    )
    assert response.status_code == 200
    assert "do not match" in response.text

    # The old password must still work — the mismatched reset must not apply.
    fresh = TestClient(logged_in.app, follow_redirects=False)
    assert (
        fresh.post(
            "/login", data={"username": "resetme", "password": "old-password-123", "next": "/"}
        ).status_code
        == 303
    )


def test_mismatched_confirmation_is_rejected_on_self_service_change(client: TestClient) -> None:
    _add_user(client, "selfmismatch", "original-password", "viewer")
    _login_as(client, "selfmismatch", "original-password")

    response = client.post(
        "/account/password",
        data={
            "current_password": "original-password",
            "new_password": "new-password-1",
            "confirm_password": "new-password-2",
        },
    )
    assert response.status_code == 400
    assert "do not match" in response.text

    # The old password must still work.
    fresh = TestClient(client.app, follow_redirects=False)
    assert (
        fresh.post(
            "/login",
            data={"username": "selfmismatch", "password": "original-password", "next": "/"},
        ).status_code
        == 303
    )
