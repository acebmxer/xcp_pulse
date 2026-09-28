"""The side navigation every signed-in page carries (base.html)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.users import create_user


def _active_links(body: str) -> int:
    return body.count('class="sidenav-link active"')


def _nav(body: str) -> str:
    start = body.index('<nav class="sidenav"')
    return body[start : body.index("</nav>", start)]


def test_every_page_link_is_listed(logged_in: TestClient) -> None:
    body = logged_in.get("/jobs").text
    assert '<nav class="sidenav"' in body
    for href in (
        "/",
        "/collect",
        "/findings",
        "/jobs",
        "/activity",
        "/redaction",
        "/update",
        "/support-package",
        "/help",
        "/settings",
        "/account",
    ):
        assert f'href="{href}"' in body, href


def test_current_page_is_the_only_one_marked_active(logged_in: TestClient) -> None:
    body = logged_in.get("/jobs").text
    assert _active_links(body) == 1
    assert 'aria-current="page" href="/jobs">Jobs</a>' in body


def test_dashboard_is_active_only_on_itself(logged_in: TestClient) -> None:
    # "/" is a prefix of every path, so it must not light up on other pages.
    assert 'aria-current="page" href="/">Dashboard</a>' in logged_in.get("/").text
    assert 'aria-current="page" href="/">Dashboard</a>' not in logged_in.get("/collect").text


def test_a_page_under_a_link_marks_that_link_active(logged_in: TestClient) -> None:
    body = logged_in.get("/help/installation").text
    assert _active_links(body) == 1
    assert 'aria-current="page" href="/help">User manual</a>' in body

    body = logged_in.get("/settings/users").text
    assert 'aria-current="page" href="/settings">Settings</a>' in body


def test_settings_link_is_admin_only(client: TestClient) -> None:
    create_user(client.app.state.db, "viewer1", "viewer-password", "viewer")
    client.post("/login", data={"username": "viewer1", "password": "viewer-password", "next": "/"})
    nav = _nav(client.get("/").text)
    assert 'href="/settings"' not in nav
    assert 'href="/account"' in nav


def test_no_side_nav_when_signed_out(client: TestClient) -> None:
    body = client.get("/login").text
    assert '<nav class="sidenav"' not in body
