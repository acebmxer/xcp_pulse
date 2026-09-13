"""The /update page: role gating, and behaviour with self-update disabled."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import hash_password
from app.users import create_user

TEST_USER = "admin"
TEST_PASSWORD = "correct-horse-battery"


def _settings(tmp_path: Path, *, enable_self_update: bool, is_dev_build: bool = False) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_user=TEST_USER,
        admin_password_hash=hash_password(TEST_PASSWORD),
        secret_key="test-secret-key-not-for-production",
        https_only=False,
        enable_https=False,
        session_hours=12,
        login_max_attempts=5,
        login_lockout_minutes=15,
        log_level="WARNING",
        enable_self_update=enable_self_update,
        compose_project_dir="",
        is_dev_build=is_dev_build,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(_settings(tmp_path, enable_self_update=True))
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    response = client.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"}
    )
    assert response.status_code == 303
    return client


def _login_as(client: TestClient, username: str, password: str) -> None:
    response = client.post("/login", data={"username": username, "password": password, "next": "/"})
    assert response.status_code == 303


def test_viewer_cannot_reach_the_update_page(client: TestClient) -> None:
    create_user(client.app.state.db, "vwr", "viewer-password", "viewer")
    _login_as(client, "vwr", "viewer-password")
    assert client.get("/update").status_code == 403


def test_operator_can_reach_the_update_page(client: TestClient) -> None:
    create_user(client.app.state.db, "op", "operator-password", "operator")
    _login_as(client, "op", "operator-password")
    assert client.get("/update").status_code == 200


def test_admin_can_reach_the_update_page(logged_in: TestClient) -> None:
    assert logged_in.get("/update").status_code == 200


def test_anonymous_is_redirected_to_login(client: TestClient) -> None:
    response = client.get("/update")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_page_explains_itself_when_self_update_is_disabled(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_self_update=False))
    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
        body = client.get("/update").text
        assert "not enabled" in body.lower()
        assert "docker socket" in body.lower()


def test_check_is_a_no_op_when_self_update_is_disabled(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_self_update=False))
    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"})
        response = client.post("/update/check")
        assert response.status_code == 303
        assert response.headers["location"] == "/update"


def test_apply_refuses_when_no_update_is_available(logged_in: TestClient) -> None:
    response = logged_in.post("/update/apply")
    assert response.status_code == 303
    assert "error" in response.headers["location"]


def test_check_now_reflects_an_available_update(
    logged_in: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:new")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:old"})

    logged_in.post("/update/check")
    body = logged_in.get("/update").text
    assert "update available" in body.lower()


def test_stale_update_started_query_param_does_not_loop_forever(logged_in: TestClient) -> None:
    """?update_started=1 is what /update/apply redirects to, and it has no
    way to clear itself from the URL — nothing here rewrites it back to a
    bare /update. If the meta-refresh ever re-requests the *current* URL
    (content="3" alone) instead of the bare path, a browser that reloaded
    this page long after the update finished would refresh into the same
    stale URL forever, stuck showing "Update started" even though
    state.last_result has long since moved on. The refresh target has to be
    the bare path, not whatever query string happens to be on screen.
    """
    body = logged_in.get("/update?update_started=1").text
    assert 'url=/update"' in body
    assert 'content="3"' not in body


def test_the_refresh_tag_is_actually_inside_head(logged_in: TestClient) -> None:
    """A <meta> tag placed outside {% block head_extra %} in a template that
    extends base.html is silently dropped by Jinja and never rendered at all
    — the bug this page actually shipped with. A substring check on the
    response body can't tell "present in <head>" from "present in <body>"
    from "not present", so assert the structural fact: the tag is inside
    <head>, not merely somewhere in the page.
    """
    body = logged_in.get("/update?update_started=1").text
    head = re.search(r"<head>(.*?)</head>", body, re.DOTALL)
    assert head is not None
    assert 'http-equiv="refresh"' in head.group(1)


@pytest.fixture
def dev_client(tmp_path: Path) -> TestClient:
    app = create_app(_settings(tmp_path, enable_self_update=True, is_dev_build=True))
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post(
            "/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"}
        )
        yield test_client


def test_dev_build_page_explains_itself_and_never_reports_available(
    dev_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real, differing deployed digest — the exact case that would report
    # "available" on a normal deployment.
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:published")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:local-build"})

    dev_client.post("/update/check")
    body = dev_client.get("/update").text
    assert "local dev build" in body.lower()
    # The actual status banner, not the explanatory prose (which mentions the
    # phrase "update available" while explaining why it won't say that).
    assert "up to date" in body.lower()
    assert "Update available." not in body
    assert "run update anyway" in body.lower()


def test_run_update_anyway_is_not_offered_on_a_normal_deployment(logged_in: TestClient) -> None:
    assert "run update anyway" not in logged_in.get("/update").text.lower()


def test_apply_anyway_is_a_no_op_on_a_non_dev_build(logged_in: TestClient) -> None:
    """Hitting the route directly, bypassing the hidden button, must still refuse."""
    response = logged_in.post("/update/apply-anyway")
    assert response.status_code == 303
    assert response.headers["location"] == "/update"


def test_apply_anyway_starts_an_update_on_a_dev_build(dev_client: TestClient) -> None:
    response = dev_client.post("/update/apply-anyway")
    assert response.status_code == 303
    assert "update_started" in response.headers["location"]


def test_apply_anyway_confirm_page_is_a_no_op_on_a_non_dev_build(logged_in: TestClient) -> None:
    """Hitting the confirmation page directly must still refuse, same as the
    POST it guards — a link on the page is not the only way in."""
    response = logged_in.get("/update/apply-anyway")
    assert response.status_code == 303
    assert response.headers["location"] == "/update"


def test_apply_anyway_confirm_page_warns_before_running_anything(dev_client: TestClient) -> None:
    """The confirm page must not itself start the update — only its own form
    submit (a separate POST) does that."""
    response = dev_client.get("/update/apply-anyway")
    assert response.status_code == 200
    assert "does not preserve" in response.text.lower()
    assert 'action="/update/apply-anyway"' in response.text

    # Nothing ran from the GET alone.
    state = dev_client.get("/update").text
    assert "update started" not in state.lower()


def test_the_button_on_the_update_page_links_to_the_confirm_page(dev_client: TestClient) -> None:
    """Regression: the button used to POST straight to /update/apply-anyway
    with no confirmation in between — one click on this button was what
    replaced a real dev container with the published image. It must now be a
    link to the confirmation page, not a form that submits directly."""
    body = dev_client.get("/update").text
    assert 'href="/update/apply-anyway"' in body
