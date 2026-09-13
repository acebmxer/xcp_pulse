"""The activity log: what gets recorded, and who can see it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.activity import list_activity, log_activity
from app.users import create_user


def test_login_is_logged(client: TestClient) -> None:
    from tests.conftest import TEST_PASSWORD, TEST_USER

    client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
    entries = list_activity(client.app.state.db)
    assert any(e.action == "login" and e.username == TEST_USER for e in entries)


def test_logout_is_logged(logged_in: TestClient) -> None:
    logged_in.post("/logout")
    entries = list_activity(logged_in.app.state.db)
    assert any(e.action == "logout" for e in entries)


def test_redaction_rule_change_is_logged(logged_in: TestClient) -> None:
    logged_in.post("/redaction/rules", data={"rule": []})
    entries = list_activity(logged_in.app.state.db)
    assert any(e.action == "redaction.rules" for e in entries)


def test_user_created_is_logged_with_role_in_detail(logged_in: TestClient) -> None:
    logged_in.post(
        "/settings/users",
        data={
            "new_username": "newbie",
            "password": "newbie-password",
            "confirm_password": "newbie-password",
            "role": "operator",
        },
    )
    entries = list_activity(logged_in.app.state.db)
    matches = [e for e in entries if e.action == "user.create"]
    assert matches
    assert "newbie" in matches[0].detail
    assert "operator" in matches[0].detail


def test_activity_page_lists_entries_newest_first(logged_in: TestClient) -> None:
    conn = logged_in.app.state.db
    log_activity(conn, "someone", "test.first", detail="oldest")
    log_activity(conn, "someone", "test.second", detail="newest")

    body = logged_in.get("/activity").text
    assert body.index("test.second") < body.index("test.first")


def test_viewer_cannot_see_the_activity_log(client: TestClient) -> None:
    conn = client.app.state.db
    create_user(conn, "vwr", "viewer-password", "viewer")
    client.post("/login", data={"username": "vwr", "password": "viewer-password", "next": "/"})
    assert client.get("/activity").status_code == 403
