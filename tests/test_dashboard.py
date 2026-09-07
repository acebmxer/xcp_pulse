"""The dashboard, and what it shows for each state of the connection.

The four states are distinct on purpose: no connection at all, a connection
that cannot be reached, a connection whose account can see nothing, and a
working one. The third is the subtle case — Xen Orchestra answers an account
without privileges with 200 and an empty list, so "nothing to show" must not
be reported as success with an empty pool list.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.xo_client import Host, Inventory, Pool, XoError
from app.xo_connection import save_connection

POOL = Pool(id="pool-1", name="xcp-ng-Pool1", master_id="host-1")
HOSTS = [
    Host(
        id="host-1",
        name="xcp-ng-host1",
        address="10.100.2.10",
        version="8.3.0",
        product="XCP-ng",
        power_state="Running",
        pool_id="pool-1",
        memory_used=38540980224,
        memory_total=103079215104,
        cpu_cores=28,
    ),
    Host(
        id="host-2",
        name="xcp-ng-host2",
        address="10.100.2.11",
        version="8.3.0",
        product="XCP-ng",
        power_state="Halted",
        enabled=False,
        pool_id="pool-1",
    ),
]


@pytest.fixture
def connected(logged_in: TestClient) -> Iterator[TestClient]:
    """A logged-in client with a stored XO connection."""
    app = logged_in.app  # type: ignore[attr-defined]
    save_connection(
        app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=app.state.settings.secret_key,
    )
    yield logged_in


def test_dashboard_without_a_connection_points_at_settings(logged_in: TestClient) -> None:
    body = logged_in.get("/").text
    assert "No Xen Orchestra connection configured" in body
    assert "/settings" in body


def test_dashboard_lists_pools_and_hosts(connected: TestClient) -> None:
    inventory = Inventory(pools=[POOL], hosts=HOSTS)
    with patch("app.routes.dashboard.build_client") as build:
        build.return_value.inventory.return_value = inventory
        body = connected.get("/").text

    assert "xcp-ng-Pool1" in body
    assert "xcp-ng-host1" in body
    assert "10.100.2.10" in body
    assert "XCP-ng 8.3.0" in body
    assert "28 cores" in body
    assert "37% memory" in body
    assert "No Xen Orchestra connection configured" not in body


def test_dashboard_marks_the_pool_master_and_disabled_hosts(connected: TestClient) -> None:
    inventory = Inventory(pools=[POOL], hosts=HOSTS)
    with patch("app.routes.dashboard.build_client") as build:
        build.return_value.inventory.return_value = inventory
        body = connected.get("/").text

    assert "master" in body
    assert "disabled" in body


def test_dashboard_explains_an_empty_inventory(connected: TestClient) -> None:
    """An account that can see nothing must not look like an empty pool.

    XO answers a privilege-less account with 200 and [], so the page has to say
    that missing privileges are the likely cause rather than showing an empty
    but apparently healthy inventory.
    """
    with patch("app.routes.dashboard.build_client") as build:
        build.return_value.inventory.return_value = Inventory()
        body = connected.get("/").text

    assert "Nothing visible to this account" in body
    assert "lacks read access" in body


def test_dashboard_shows_a_connection_failure(connected: TestClient) -> None:
    with patch("app.routes.dashboard.build_client") as build:
        build.return_value.inventory.side_effect = XoError("cannot reach https://xo.example.com")
        response = connected.get("/")

    assert response.status_code == 200, "an unreachable XO must not break the page"
    assert "cannot reach https://xo.example.com" in response.text


def test_dashboard_reports_a_changed_secret_key(connected: TestClient) -> None:
    """The token was encrypted under a key that no longer exists.

    The fix is to re-enter the token, which is a different action from fixing
    an unreachable address, so the two are not merged into one message.
    """
    app = connected.app  # type: ignore[attr-defined]
    save_connection(
        app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key="the-key-that-was-in-use-when-this-was-saved",
    )

    body = connected.get("/").text
    assert "secret key has changed" in body
    assert "Settings" in body


def test_dashboard_requires_login(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
