"""The Refresh inventory job: what it stores, and what reads it back.

The round trip matters more than either half. The dashboard shows what this
job stored, so a job that writes something the reader cannot rebuild would
leave the page empty with a job marked successful — which is exactly the
failure a stored result is supposed to prevent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from app.artifacts import list_for_job
from app.db import init_db
from app.job_inventory import INVENTORY_ARTIFACT, KIND, inventory_from_job
from app.job_runner import JobWorker
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job
from app.xo_client import Host, Inventory, Pool, XoError
from app.xo_connection import save_connection

SECRET = "test-secret-key-not-for-production"

POOL = Pool(id="pool-1", name="Pool1", master_id="host-1")
HOST = Host(
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
    cpu_sockets=2,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "test.db")
    save_connection(
        conn,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=SECRET,
    )
    return conn


class _Settings:
    secret_key = SECRET


@pytest.fixture
def worker(tmp_path: Path) -> JobWorker:
    return JobWorker(tmp_path / "test.db", tmp_path, _Settings())


def _run(conn: sqlite3.Connection, worker: JobWorker, inventory: Inventory) -> str:
    job = enqueue(conn, KIND)
    with patch("app.job_inventory.build_client") as build:
        build.return_value.inventory.return_value = inventory
        worker.run_one(conn)
    return job.id


def test_the_inventory_is_stored_as_an_artifact(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job_id = _run(conn, worker, Inventory(pools=[POOL], hosts=[HOST]))

    assert get_job(conn, job_id).state == SUCCEEDED
    assert [item.name for item in list_for_job(conn, job_id)] == [INVENTORY_ARTIFACT]


def test_what_is_stored_rebuilds_into_the_same_inventory(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The round trip the dashboard depends on, field for field."""
    job_id = _run(conn, worker, Inventory(pools=[POOL], hosts=[HOST]))

    rebuilt = inventory_from_job(conn, tmp_path, job_id)
    assert rebuilt.pools == [POOL]
    assert rebuilt.hosts == [HOST]
    assert rebuilt.hosts[0].memory_percent == 37
    assert rebuilt.hosts_in("pool-1") == [HOST]


def test_an_empty_inventory_is_stored_rather_than_treated_as_a_failure(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """XO answers a privilege-less account with 200 and [].

    That is a real answer, so the job succeeds and stores it; the page decides
    how to explain it. Failing the job here would report a privileges problem
    as a connection problem.
    """
    job_id = _run(conn, worker, Inventory())

    assert get_job(conn, job_id).state == SUCCEEDED
    assert inventory_from_job(conn, tmp_path, job_id).is_empty


def test_an_unreachable_xo_fails_the_job_with_the_reason(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job = enqueue(conn, KIND)
    with patch("app.job_inventory.build_client") as build:
        build.side_effect = XoError("cannot reach https://xo.example.com")
        worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "cannot reach" in stored.error


def test_no_configured_connection_fails_the_job_with_its_own_reason(
    tmp_path: Path, worker: JobWorker
) -> None:
    """A different fix from an unreachable address, so a different message."""
    conn = init_db(tmp_path / "test.db")
    job = enqueue(conn, KIND)
    worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "no xen orchestra connection" in stored.error.lower()


def test_a_job_with_no_artifact_rebuilds_as_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A job that failed before storing anything must not raise on read."""
    job = enqueue(conn, KIND)
    assert inventory_from_job(conn, tmp_path, job.id) is None


def test_an_artifact_missing_a_field_still_loads(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """An artifact written by an older version must not break the page.

    Unknown keys are dropped and absent ones left at their dataclass default,
    so a stored result outliving a change to Pool or Host still renders.
    """
    from app.artifacts import store_json

    job = enqueue(conn, KIND)
    store_json(
        conn,
        tmp_path,
        job_id=job.id,
        name=INVENTORY_ARTIFACT,
        payload={
            "pools": [{"id": "pool-1", "name": "Pool1", "a_field_we_dropped": 1}],
            "hosts": [{"id": "host-1", "name": "host1"}],
        },
    )

    rebuilt = inventory_from_job(conn, tmp_path, job.id)
    assert rebuilt.pools[0].name == "Pool1"
    assert rebuilt.pools[0].master_id == ""
    assert rebuilt.hosts[0].enabled is True
