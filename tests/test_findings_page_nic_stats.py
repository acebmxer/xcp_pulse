"""The Findings page's "Read NIC statistics" section."""

from __future__ import annotations

import socket
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from app.artifacts import store_json
from app.findings import CRITICAL, Finding, Report, SourceResult
from app.job_inventory import INVENTORY_ARTIFACT
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_nic_stats import KIND as NIC_KIND
from app.jobs import enqueue, mark_succeeded
from app.ssh_connection import save_connection
from tests.helpers import run_pending_jobs

PRIVATE_KEY = (
    ed25519.Ed25519PrivateKey.generate()
    .private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    .decode("ascii")
)

NIC_REPORT = Report(
    findings=[
        Finding(
            severity=CRITICAL,
            title="eth4 on xcp-ng-host1 has recorded packet errors",
            evidence="rx_crc_errors: 3",
            action="Check the cable and switch port.",
            source="nic_stats",
            object_id="xcp-ng-host1/eth4",
        )
    ],
    sources=[SourceResult("nic_stats", read=True, examined=1, detail="checked xcp-ng-host1")],
    window_days=0,
    created_at=1788700000.0,
)


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _configure(client: TestClient, *, port: int | None = None) -> None:
    save_connection(
        client.app.state.db,
        private_key=PRIVATE_KEY,
        passphrase="",
        port=port or _unused_local_port(),
        secret_key=client.app.state.settings.secret_key,
    )


def _seed_host(client: TestClient, *, host_id: str = "host-1", address: str = "127.0.0.1") -> None:
    db = client.app.state.db
    data_dir = client.app.state.settings.data_dir
    job = enqueue(db, INVENTORY_KIND, {})
    store_json(
        db,
        data_dir,
        job_id=job.id,
        name=INVENTORY_ARTIFACT,
        payload={
            "pools": [],
            "hosts": [{"id": host_id, "name": "xcp-ng-host1", "address": address}],
        },
    )
    mark_succeeded(db, job.id)


def _store_nic(client: TestClient, report: Report = NIC_REPORT) -> None:
    """Run a NIC statistics job whose classification is stubbed, so a report
    is stored without needing a real host to SSH into."""
    _configure(client)
    _seed_host(client)
    enqueue(client.app.state.db, NIC_KIND, {"host_ids": ["host-1"]})
    with patch("app.job_nic_stats.collect_nic_stat_findings", return_value=report):
        run_pending_jobs(client.app)


def test_section_renders(logged_in: TestClient) -> None:
    body = logged_in.get("/findings").text
    assert "Read NIC statistics" in body


def test_without_a_key_the_button_is_hidden_and_says_why(logged_in: TestClient) -> None:
    _seed_host(logged_in)
    body = logged_in.get("/findings").text

    assert "not configured yet" in body
    assert 'action="/findings/nic-stats"' not in body


def test_with_a_key_but_no_inventory_says_so(logged_in: TestClient) -> None:
    _configure(logged_in)
    body = logged_in.get("/findings").text

    assert "No hosts are known yet" in body


def test_with_a_key_and_inventory_the_hosts_are_offered(logged_in: TestClient) -> None:
    _configure(logged_in)
    _seed_host(logged_in)
    body = logged_in.get("/findings").text

    assert "xcp-ng-host1" in body
    assert 'action="/findings/nic-stats"' in body


def test_starting_a_run_requires_a_login(client: TestClient) -> None:
    response = client.post("/findings/nic-stats")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_cannot_start_without_a_key(logged_in: TestClient) -> None:
    _seed_host(logged_in)
    response = logged_in.post("/findings/nic-stats", data={"host_ids": ["host-1"]})

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_cannot_start_with_no_host_picked(logged_in: TestClient) -> None:
    _configure(logged_in)
    _seed_host(logged_in)
    response = logged_in.post("/findings/nic-stats", data={})

    assert response.status_code == 303
    assert "Pick" in response.headers["location"] or "error=" in response.headers["location"]


def test_an_unknown_host_id_is_ignored_not_trusted(logged_in: TestClient) -> None:
    _configure(logged_in)
    _seed_host(logged_in)
    response = logged_in.post("/findings/nic-stats", data={"host_ids": ["not-a-real-host"]})

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_a_run_is_queued_for_a_real_known_host(logged_in: TestClient) -> None:
    _configure(logged_in)
    _seed_host(logged_in)
    response = logged_in.post("/findings/nic-stats", data={"host_ids": ["host-1"]})

    assert response.status_code == 303
    assert "notice=" in response.headers["location"]


def test_a_second_run_is_refused_while_one_is_going(logged_in: TestClient) -> None:
    _configure(logged_in)
    _seed_host(logged_in)
    enqueue(logged_in.app.state.db, NIC_KIND, {"host_ids": ["host-1"]})

    response = logged_in.post("/findings/nic-stats", data={"host_ids": ["host-1"]})

    assert "already" in response.headers["location"]


def test_the_stored_report_renders_with_evidence_and_action(logged_in: TestClient) -> None:
    _store_nic(logged_in)
    body = logged_in.get("/findings").text

    assert "eth4 on xcp-ng-host1 has recorded packet errors" in body
    assert "rx_crc_errors: 3" in body
    assert "Check the cable and switch port." in body


def test_a_clean_report_says_so(logged_in: TestClient) -> None:
    _store_nic(logged_in, Report(sources=[SourceResult("nic_stats", read=True)], window_days=0))
    body = logged_in.get("/findings").text

    assert "No packet errors were found" in body
