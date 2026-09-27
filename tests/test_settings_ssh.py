"""The Settings page's Host SSH connection form."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from app.artifacts import store_json
from app.job_inventory import INVENTORY_ARTIFACT
from app.job_inventory import KIND as INVENTORY_KIND
from app.jobs import enqueue, mark_succeeded
from app.ssh_client import PING_REPLY

PRIVATE_KEY = (
    ed25519.Ed25519PrivateKey.generate()
    .private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    .decode("ascii")
)


def _seed_host(client: TestClient, *, address: str = "203.0.113.5") -> None:
    """Store a stand-in "last successful refresh" so the settings page's test
    route has a host to try — the same shape ``job_inventory.run`` writes."""
    app = client.app
    conn = app.state.db
    data_dir = app.state.settings.data_dir
    job = enqueue(conn, INVENTORY_KIND, {})
    store_json(
        conn,
        data_dir,
        job_id=job.id,
        name=INVENTORY_ARTIFACT,
        payload={
            "pools": [],
            "hosts": [{"id": "host-1", "name": "xcp-ng-host1", "address": address}],
        },
    )
    mark_succeeded(conn, job.id)


@pytest.fixture
def saved_key(logged_in: TestClient) -> Iterator[TestClient]:
    logged_in.post(
        "/settings/ssh",
        data={"private_key": PRIVATE_KEY, "passphrase": "", "port": "22"},
    )
    yield logged_in


def test_ssh_section_renders(logged_in: TestClient) -> None:
    response = logged_in.get("/settings")
    assert response.status_code == 200
    assert "Host SSH connection" in response.text


def test_saving_a_key_redirects_and_stores_it(logged_in: TestClient) -> None:
    response = logged_in.post(
        "/settings/ssh",
        data={"private_key": PRIVATE_KEY, "passphrase": "", "port": "2222"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?ssh_saved=1"

    page = logged_in.get("/settings")
    assert "2222" in page.text
    assert PRIVATE_KEY not in page.text


def test_saving_with_an_empty_key_is_rejected(logged_in: TestClient) -> None:
    response = logged_in.post(
        "/settings/ssh", data={"private_key": "   ", "passphrase": "", "port": "22"}
    )
    assert response.status_code == 400


def test_deleting_the_connection(saved_key: TestClient) -> None:
    response = saved_key.post("/settings/ssh/delete")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?ssh_deleted=1"

    page = saved_key.get("/settings")
    assert "No SSH connection is configured yet." in page.text


def test_testing_without_a_saved_key(logged_in: TestClient) -> None:
    response = logged_in.post("/settings/ssh/test")
    assert response.status_code == 400
    assert "Save an SSH key" in response.text


def test_testing_without_an_inventory(saved_key: TestClient) -> None:
    """No NIC statistics interfaces configured either — testing the
    connection must not depend on any specific check being set up."""
    response = saved_key.post("/settings/ssh/test")
    assert response.status_code == 400
    assert "Refresh inventory" in response.text


def _unused_local_port() -> int:
    """A port nothing listens on, so a connection to it is refused instantly
    rather than hanging on a black-holed address for the connect timeout."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_testing_reaches_for_a_real_host_and_fails_cleanly(logged_in: TestClient) -> None:
    """No SSH server is listening in a test — this proves the route gets as
    far as attempting a real connection rather than a fixed error."""
    port = _unused_local_port()
    logged_in.post(
        "/settings/ssh",
        data={"private_key": PRIVATE_KEY, "passphrase": "", "port": str(port)},
    )
    _seed_host(logged_in, address="127.0.0.1")

    response = logged_in.post("/settings/ssh/test")

    assert response.status_code == 200
    assert "xcp-ng-host1" in response.text
    assert "Connection failed" in response.text


def test_testing_succeeds_with_no_check_configured(saved_key: TestClient) -> None:
    """Test connection uses the dispatcher script's own ping probe, so it
    works with no NIC statistics interfaces (or any other check) set up."""
    _seed_host(saved_key)

    with patch("app.routes.settings.run_check", return_value=(PING_REPLY, False)):
        response = saved_key.post("/settings/ssh/test")

    assert response.status_code == 200
    assert "Connection working" in response.text
    assert "xcp-ng-host1" in response.text


def test_ssh_routes_require_login(client: TestClient) -> None:
    for method, path in (
        ("post", "/settings/ssh"),
        ("post", "/settings/ssh/test"),
        ("post", "/settings/ssh/delete"),
    ):
        response = getattr(client, method)(path, data={})
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
