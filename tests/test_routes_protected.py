"""Every route that is not deliberately public must require a session.

This walks the app's own route table rather than a hand-written list, so a
route added later is covered without anyone remembering to add it here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Public by design, each for a stated reason.
PUBLIC_PATHS = {
    "/healthz",  # compose healthcheck, which has no session
    "/login",  # the way in
    "/logout",  # clearing a session must work even once it has expired
}


def _collect(routes: Iterable[object], found: set[str]) -> None:
    """Walk the route tree, descending into included routers.

    FastAPI 0.141 wraps each include_router() call in a router object rather
    than flattening its routes into app.routes, so a single pass over
    app.routes finds no APIRoute at all. Descending via original_router keeps
    this working across both shapes.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            if "GET" in route.methods:
                found.add(route.path)
            continue
        nested = getattr(route, "original_router", None) or getattr(route, "routes", None)
        if nested is not None:
            _collect(getattr(nested, "routes", nested), found)


def _app_get_routes(client: TestClient) -> list[str]:
    found: set[str] = set()
    _collect(client.app.routes, found)
    return sorted(found)


def test_every_non_public_get_route_requires_login(client: TestClient) -> None:
    checked = 0
    for path in _app_get_routes(client):
        if path in PUBLIC_PATHS or "{" in path:
            continue
        response = client.get(path)
        assert response.status_code == 303, f"{path} did not redirect an anonymous caller"
        assert response.headers["location"].startswith("/login"), path
        checked += 1

    assert checked > 0, "no protected routes were exercised — the check is not working"


def test_dashboard_renders_once_logged_in(logged_in: TestClient) -> None:
    response = logged_in.get("/")
    assert response.status_code == 200
    assert "No Xen Orchestra connection configured" in response.text


def test_anonymous_dashboard_redirect_preserves_destination(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"


def test_dashboard_keeps_the_roadmap_section(logged_in: TestClient) -> None:
    """ "What is coming" is a permanent part of the dashboard.

    It stays until Nick decides otherwise — it is not scaffolding to be removed
    once the dashboard has real content. This test exists so that removing it
    is a deliberate act rather than a tidy-up nobody notices.
    """
    body = logged_in.get("/").text
    assert "What is coming" in body
    # A heading with no stages under it would be the same loss by another route.
    # Match the row class exactly: 'class="stage"' and 'class="stage ...'
    # rather than the prefix, which also hits stages-group and stage-name.
    rows = re.findall(r'class="stage(?:\s[^"]*)?"', body)
    assert len(rows) >= 5, f"expected the stage rows to still be listed, found {len(rows)}"
