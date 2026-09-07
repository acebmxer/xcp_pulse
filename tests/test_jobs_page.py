"""The jobs page: starting, watching and cancelling work from the browser."""

from __future__ import annotations

import re
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.artifacts import list_for_job
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_redact import KIND as REDACT_KIND
from app.jobs import claim_next, enqueue, get_job, list_jobs, mark_failed, mark_succeeded
from app.redact import RULES, set_enabled_rules
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


# ---- redaction from the jobs page ----
#
# What matters here is not that a job was queued — that is the same enqueue
# every other job uses — but that the report reaches the page. The report is
# the whole reason the job exists, and a job that succeeds while the page shows
# nothing is indistinguishable from one that masked nothing.


def _inventory_artifact(app) -> str:
    """Run an inventory refresh and return the artifact it stored.

    Deliberately a real job rather than a hand-written row: the file the page
    offers for redaction has to be one the store actually holds.
    """
    enqueue(app.state.db, INVENTORY_KIND)
    with patch("app.job_inventory.build_client") as build:
        build.return_value.inventory.return_value = Inventory(pools=[Pool("p1", "Pool1")])
        run_pending_jobs(app)
    job = list_jobs(app.state.db, kind=INVENTORY_KIND)[0]
    return list_for_job(app.state.db, job.id)[0].id


def test_with_nothing_stored_the_page_says_there_is_nothing_to_redact(
    logged_in: TestClient,
) -> None:
    assert "Nothing is stored to redact yet" in logged_in.get("/jobs").text


def test_a_stored_file_is_offered_for_redaction(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)

    body = connected.get("/jobs").text
    assert f'value="{artifact_id}"' in body


def test_redacting_a_stored_file_shows_the_report_on_the_page(
    connected: TestClient,
) -> None:
    """The counts, per rule, for a whole run — what this feature is for."""
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)

    assert connected.post("/jobs/redact", data={"artifact_id": artifact_id}).status_code == 303
    run_pending_jobs(app)

    body = connected.get("/jobs").text
    assert "redaction-report.json" in body
    assert "inventory.redacted.json" in body
    # Every rule gets a row, including the ones that matched nothing.
    for rule in RULES:
        assert rule.title in body


def test_a_rule_switched_off_is_marked_off_in_the_report(connected: TestClient) -> None:
    """A zero and a rule that never ran must not read the same on the page."""
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    set_enabled_rules(app.state.db, [rule.name for rule in RULES if rule.name != "uuid"])

    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    assert "rule-off" in connected.get("/jobs").text


def test_redacting_an_artifact_that_is_gone_says_so(connected: TestClient) -> None:
    """A bad id is a message on the page, not a failed job to go and read."""
    app = connected.app  # type: ignore[attr-defined]
    response = connected.post("/jobs/redact", data={"artifact_id": "no-such-artifact"})

    assert "no+longer+stored" in response.headers["location"]
    assert list_jobs(app.state.db, kind=REDACT_KIND) == []


def test_a_second_redaction_is_refused_while_one_is_pending(connected: TestClient) -> None:
    """A second redaction of the same file produces nothing the first will not.

    The pending job is enqueued directly rather than through the route, so it
    stays queued for the second request to be refused against. The app's worker
    thread is already stopped by ``run_pending_jobs`` inside
    ``_inventory_artifact``; otherwise it would finish this one first and the
    test would measure timing rather than the guard.
    """
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)

    enqueue(app.state.db, REDACT_KIND, {"artifact_id": artifact_id})
    response = connected.post("/jobs/redact", data={"artifact_id": artifact_id})

    assert "already+running" in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=REDACT_KIND)) == 1


def test_a_redacted_copy_is_not_offered_for_redaction_again(connected: TestClient) -> None:
    """Redacting a redacted file masks nothing and only adds a file."""
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    body = connected.get("/jobs").text
    offered = re.findall(r'<option value="([0-9a-f]{32})">([^<]+)', body)
    assert [name.split(" —")[0] for _, name in offered] == ["inventory.json"]


def test_the_redaction_form_is_disabled_when_nothing_can_be_redacted(
    logged_in: TestClient,
) -> None:
    assert 'name="artifact_id" disabled' in logged_in.get("/jobs").text
