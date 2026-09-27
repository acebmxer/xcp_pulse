"""The Settings page's Host SSH connections form."""

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
from app.ssh_connection import get_connection

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
    _seed_hosts(client, [{"id": "host-1", "name": "xcp-ng-host1", "address": address}])


def _seed_hosts(client: TestClient, hosts: list[dict]) -> None:
    app = client.app
    conn = app.state.db
    data_dir = app.state.settings.data_dir
    job = enqueue(conn, INVENTORY_KIND, {})
    store_json(
        conn,
        data_dir,
        job_id=job.id,
        name=INVENTORY_ARTIFACT,
        payload={"pools": [], "hosts": hosts},
    )
    mark_succeeded(conn, job.id)


@pytest.fixture
def saved_key(logged_in: TestClient) -> Iterator[TestClient]:
    """A host in the stored inventory with a key already saved for it."""
    _seed_host(logged_in)
    logged_in.post(
        "/settings/ssh",
        data={"host_id": "host-1", "private_key": PRIVATE_KEY, "passphrase": "", "port": "22"},
    )
    yield logged_in


def test_ssh_section_renders(logged_in: TestClient) -> None:
    response = logged_in.get("/settings")
    assert response.status_code == 200
    assert "Host SSH connections" in response.text


def test_saving_a_key_redirects_and_stores_it(logged_in: TestClient) -> None:
    _seed_host(logged_in)
    response = logged_in.post(
        "/settings/ssh",
        data={"host_id": "host-1", "private_key": PRIVATE_KEY, "passphrase": "", "port": "2222"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?ssh_saved=1"

    page = logged_in.get("/settings")
    assert "2222" in page.text
    assert PRIVATE_KEY not in page.text


def test_saving_with_an_empty_key_is_rejected(logged_in: TestClient) -> None:
    _seed_host(logged_in)
    response = logged_in.post(
        "/settings/ssh",
        data={"host_id": "host-1", "private_key": "   ", "passphrase": "", "port": "22"},
    )
    assert response.status_code == 400


def test_saving_without_a_known_host_is_rejected(logged_in: TestClient) -> None:
    response = logged_in.post(
        "/settings/ssh",
        data={
            "host_id": "no-such-host",
            "private_key": PRIVATE_KEY,
            "passphrase": "",
            "port": "22",
        },
    )
    assert response.status_code == 400
    assert "Pick a host" in response.text


def test_saving_a_second_host_does_not_overwrite_the_first(saved_key: TestClient) -> None:
    """The bug per-host keying fixes: with one shared key, saving a second
    host's key used to silently destroy the first host's stored key."""
    _seed_hosts(
        saved_key,
        [
            {"id": "host-1", "name": "xcp-ng-host1", "address": "203.0.113.5"},
            {"id": "host-2", "name": "xcp-ng-host2", "address": "203.0.113.6"},
        ],
    )
    saved_key.post(
        "/settings/ssh",
        data={"host_id": "host-2", "private_key": PRIVATE_KEY, "passphrase": "", "port": "2200"},
    )

    conn = saved_key.app.state.db
    assert get_connection(conn, "host-1").port == 22
    assert get_connection(conn, "host-2").port == 2200


def test_deleting_the_connection(saved_key: TestClient) -> None:
    response = saved_key.post("/settings/ssh/delete", data={"host_id": "host-1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?ssh_deleted=1"

    page = saved_key.get("/settings")
    assert "No SSH keys are configured yet." in page.text


def test_deleting_one_hosts_key_leaves_another_configured(saved_key: TestClient) -> None:
    _seed_hosts(
        saved_key,
        [
            {"id": "host-1", "name": "xcp-ng-host1", "address": "203.0.113.5"},
            {"id": "host-2", "name": "xcp-ng-host2", "address": "203.0.113.6"},
        ],
    )
    saved_key.post(
        "/settings/ssh",
        data={"host_id": "host-2", "private_key": PRIVATE_KEY, "passphrase": "", "port": "22"},
    )

    saved_key.post("/settings/ssh/delete", data={"host_id": "host-1"})

    conn = saved_key.app.state.db
    assert get_connection(conn, "host-1") is None
    assert get_connection(conn, "host-2") is not None


def test_testing_without_a_saved_key(logged_in: TestClient) -> None:
    _seed_host(logged_in)
    response = logged_in.post("/settings/ssh/test")
    assert response.status_code == 400
    assert "Save an SSH key" in response.text


def test_testing_without_an_inventory(saved_key: TestClient) -> None:
    """A key exists for a host, but nothing is in the inventory any more —
    testing must not depend on any specific check being set up."""
    _seed_hosts(saved_key, [])
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
    _seed_host(logged_in, address="127.0.0.1")
    logged_in.post(
        "/settings/ssh",
        data={"host_id": "host-1", "private_key": PRIVATE_KEY, "passphrase": "", "port": str(port)},
    )

    response = logged_in.post("/settings/ssh/test")

    assert response.status_code == 200
    assert "xcp-ng-host1" in response.text
    assert "Connection failed" in response.text


def test_testing_succeeds_with_no_check_configured(saved_key: TestClient) -> None:
    """Test connection uses the dispatcher script's own ping probe, so it
    works with no NIC statistics interfaces (or any other check) set up."""
    with patch("app.routes.settings.run_check", return_value=(PING_REPLY, False)):
        response = saved_key.post("/settings/ssh/test")

    assert response.status_code == 200
    assert "Connection working" in response.text
    assert "xcp-ng-host1" in response.text


def test_testing_can_target_a_chosen_host_instead_of_the_alphabetically_first(
    saved_key: TestClient,
) -> None:
    """An operator adding a second host must be able to test that one, not
    only whichever host happens to sort first — see settings.html's picker."""
    _seed_hosts(
        saved_key,
        [
            {"id": "host-1", "name": "aaa-host", "address": "203.0.113.5"},
            {"id": "host-2", "name": "zzz-host", "address": "203.0.113.6"},
        ],
    )
    saved_key.post(
        "/settings/ssh",
        data={"host_id": "host-2", "private_key": PRIVATE_KEY, "passphrase": "", "port": "22"},
    )

    with patch("app.routes.settings.run_check", return_value=(PING_REPLY, False)) as run_check:
        response = saved_key.post("/settings/ssh/test", data={"host_id": "host-2"})

    assert response.status_code == 200
    assert "Connected to zzz-host" in response.text
    assert run_check.call_args.kwargs["host"] == "203.0.113.6"


def test_testing_defaults_to_the_alphabetically_first_host_with_no_choice(
    saved_key: TestClient,
) -> None:
    _seed_hosts(
        saved_key,
        [
            {"id": "host-1", "name": "aaa-host", "address": "203.0.113.5"},
            {"id": "host-2", "name": "zzz-host", "address": "203.0.113.6"},
        ],
    )

    with patch("app.routes.settings.run_check", return_value=(PING_REPLY, False)):
        response = saved_key.post("/settings/ssh/test")

    assert "Connected to aaa-host" in response.text


def test_testing_a_host_id_no_longer_in_the_inventory_is_refused(
    saved_key: TestClient,
) -> None:
    response = saved_key.post("/settings/ssh/test", data={"host_id": "gone"})

    assert response.status_code == 400
    assert "no longer in the stored inventory" in response.text


def test_ssh_routes_require_login(client: TestClient) -> None:
    for method, path in (
        ("post", "/settings/ssh"),
        ("post", "/settings/ssh/test"),
        ("post", "/settings/ssh/delete"),
    ):
        response = getattr(client, method)(path, data={})
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
