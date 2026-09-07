"""The jobs page: starting, watching and cancelling work from the browser."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.job_inventory import KIND as INVENTORY_KIND
from app.jobs import claim_next, enqueue, get_job, list_jobs, mark_failed, mark_succeeded
from app.xo_client import Inventory, Pool
from app.xo_connection import save_connection
from tests.helpers import run_pending_jobs


@pytest.fixture
def connected(logged_in: TestClient) -> Iterator[TestClient]:
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


def test_the_jobs_page_requires_login(client: TestClient) -> None:
    assert client.get("/jobs").status_code == 303


def test_an_empty_history_says_so(logged_in: TestClient) -> None:
    assert "Nothing has run yet" in logged_in.get("/jobs").text


def test_refresh_queues_a_job(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    response = connected.post("/jobs/refresh-inventory")

    assert response.status_code == 303
    assert len(list_jobs(app.state.db, kind=INVENTORY_KIND)) == 1


def test_refresh_without_a_connection_says_to_configure_one(logged_in: TestClient) -> None:
    """Queueing a job that can only fail wastes the operator's time."""
    app = logged_in.app  # type: ignore[attr-defined]
    response = logged_in.post("/jobs/refresh-inventory")

    assert "Configure" in response.headers["location"]
    assert list_jobs(app.state.db, kind=INVENTORY_KIND) == []


def test_a_second_refresh_is_refused_while_one_is_pending(connected: TestClient) -> None:
    """Queueing another identical refresh produces nothing the first will not."""
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/jobs/refresh-inventory")
    response = connected.post("/jobs/refresh-inventory")

    assert "already+running" in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=INVENTORY_KIND)) == 1


def test_a_finished_job_and_its_artifact_are_listed(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    enqueue(app.state.db, INVENTORY_KIND)
    with patch("app.job_inventory.build_client") as build:
        build.return_value.inventory.return_value = Inventory(pools=[Pool("p1", "Pool1")])
        run_pending_jobs(app)

    body = connected.get("/jobs").text
    assert "succeeded" in body
    assert "inventory.json" in body


def test_a_failed_job_shows_its_error(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    job = enqueue(app.state.db, INVENTORY_KIND)
    claim_next(app.state.db)
    mark_failed(app.state.db, job.id, "cannot reach https://xo.example.com")

    body = connected.get("/jobs").text
    assert "failed" in body
    assert "cannot reach https://xo.example.com" in body


def test_a_running_job_can_be_cancelled(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    job = enqueue(app.state.db, INVENTORY_KIND)
    claim_next(app.state.db)

    response = connected.post(f"/jobs/{job.id}/cancel")
    assert response.status_code == 303
    assert get_job(app.state.db, job.id).cancel_requested is True


def test_cancelling_a_finished_job_says_it_was_too_late(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    job = enqueue(app.state.db, INVENTORY_KIND)
    claim_next(app.state.db)
    mark_succeeded(app.state.db, job.id)

    response = connected.post(f"/jobs/{job.id}/cancel")
    assert "already+finished" in response.headers["location"]


def test_the_status_endpoint_reports_progress(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    job = enqueue(app.state.db, INVENTORY_KIND)

    payload = connected.get(f"/jobs/{job.id}/status").json()
    assert payload["state"] == "queued"
    assert payload["active"] is True
    assert payload["progress"] == 0


def test_the_status_endpoint_404s_for_an_unknown_job(connected: TestClient) -> None:
    assert connected.get("/jobs/not-a-job/status").status_code == 404


def test_the_status_endpoint_requires_login(client: TestClient) -> None:
    assert client.get("/jobs/anything/status").status_code == 303


def test_the_page_only_auto_refreshes_while_something_is_running(
    connected: TestClient,
) -> None:
    """A settled list reloading itself for ever is a page that never stops
    fetching, for nothing to look at."""
    app = connected.app  # type: ignore[attr-defined]
    job = enqueue(app.state.db, INVENTORY_KIND)
    assert 'http-equiv="refresh"' in connected.get("/jobs").text

    claim_next(app.state.db)
    mark_succeeded(app.state.db, job.id)
    assert 'http-equiv="refresh"' not in connected.get("/jobs").text
