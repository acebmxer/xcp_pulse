"""The User manual's routes: /help, /help/{slug}, /help/search."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_help_index_renders_the_first_user_guide_page(logged_in: TestClient) -> None:
    response = logged_in.get("/help")
    assert response.status_code == 200
    body = response.text
    assert "XCP Pulse" in body
    # The sidebar lists the User guide group and every top-level page.
    assert "User guide" in body
    assert "First login" in body
    assert "Installation" in body
    assert "Configuration" in body
    assert "Architecture" in body
    # README and the function index are GitHub-only, not part of the manual.
    assert "Function index" not in body
    assert 'href="/help/readme"' not in body
    assert 'href="/help/functions"' not in body
    assert 'href="/help/roadmap"' not in body


def test_help_page_renders_one_doc(logged_in: TestClient) -> None:
    response = logged_in.get("/help/installation")
    assert response.status_code == 200
    body = response.text
    assert "Requirements" in body
    assert 'class="docs-nav-link active"' in body


def test_help_renders_a_user_guide_page(logged_in: TestClient) -> None:
    response = logged_in.get("/help/first-login")
    assert response.status_code == 200
    assert "First login and connecting to Xen Orchestra" in response.text


def test_help_user_guide_group_is_open_when_one_of_its_pages_is_active(
    logged_in: TestClient,
) -> None:
    response = logged_in.get("/help/collect")
    assert response.status_code == 200
    # Jinja renders the boolean `open` attribute right after the tag name
    # when present; its absence is the only other way this can render.
    assert "<details open>" in response.text


def test_help_user_guide_group_is_open_by_default_on_the_index(logged_in: TestClient) -> None:
    response = logged_in.get("/help")
    assert "<details open>" in response.text


def test_help_user_guide_group_is_collapsed_on_a_top_level_page(logged_in: TestClient) -> None:
    response = logged_in.get("/help/installation")
    assert "<details>" in response.text
    assert "<details open>" not in response.text


def test_help_unknown_slug_shows_an_error_not_a_500(logged_in: TestClient) -> None:
    response = logged_in.get("/help/nonexistent")
    assert response.status_code == 200
    assert "does not exist" in response.text


def test_help_search_finds_a_match(logged_in: TestClient) -> None:
    response = logged_in.get("/help/search", params={"q": "redaction"})
    assert response.status_code == 200
    assert "Search results" in response.text


def test_help_search_with_no_match_says_so(logged_in: TestClient) -> None:
    response = logged_in.get("/help/search", params={"q": "xyzzy-not-a-real-word"})
    assert response.status_code == 200
    assert "No pages match" in response.text


def test_help_requires_login(client: TestClient) -> None:
    response = client.get("/help")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_fastapi_api_docs_still_disabled(client: TestClient) -> None:
    # /help exists now, but FastAPI's own reserved /docs path must not have
    # been reclaimed by it.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} should not be served"


def test_nav_link_says_user_manual(logged_in: TestClient) -> None:
    response = logged_in.get("/help")
    assert 'href="/help">User manual</a>' in response.text
