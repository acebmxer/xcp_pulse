"""The "Collect XO diagnostics" card on the Collect page.

Stage 2 of the XO API diagnostics feature, rebuilt to match the plan: its own
independent card on the existing Collect page (not a separate page/nav item),
three source checkboxes, and per-source raw/redacted artifacts read back
through the same widgets a host collection already renders with.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.artifacts import list_for_job
from app.job_diagnostics import KIND as DIAGNOSTICS_KIND
from app.jobs import enqueue, list_jobs
from app.xo_client import XoError
from app.xo_connection import save_connection
from tests.helpers import run_pending_jobs


class _FakeClient:
    def __init__(self, **records):
        self._records = records

    def users(self):
        return self._records.get("users", [])

    def backup_logs(self, since):
        return self._records.get("backups", [])

    def restore_logs(self, since):
        return self._records.get("restores", [])

    def tasks(self, since):
        return self._records.get("tasks", [])

    def messages(self, since):
        return self._records.get("messages", [])

    def alarms(self, since):
        return self._records.get("alarms", [])

    def backup_log_detail(self, log_id):
        return self._records.get("detail", {}).get(log_id, {})

    def restore_log_detail(self, log_id):
        return self.backup_log_detail(log_id)


@pytest.fixture
def connected(logged_in: TestClient):
    save_connection(
        logged_in.app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=logged_in.app.state.settings.secret_key,
    )
    yield logged_in


def _collect_diagnostics(client: TestClient, **records) -> str:
    app = client.app  # type: ignore[attr-defined]
    client.post(
        "/collect/diagnostics",
        data={"sources": ["backup_restore", "tasks", "messages_alarms"]},
    )
    with patch("app.job_diagnostics.build_client", return_value=_FakeClient(**records)):
        run_pending_jobs(app)
    return list_jobs(app.state.db, kind=DIAGNOSTICS_KIND, limit=1)[0].id


def test_the_card_is_offered_without_needing_an_inventory(connected: TestClient) -> None:
    """Unlike the host collection card, this one reads instance-wide, so it
    needs no stored inventory and no host picker."""
    body = connected.get("/collect").text
    assert "Collect XO diagnostics" in body
    assert 'name="sources" value="backup_restore"' in body


def test_without_a_connection_the_button_is_disabled(logged_in: TestClient) -> None:
    body = logged_in.get("/collect").text
    assert "Collect XO diagnostics" in body
    assert 'action="/collect/diagnostics"' in body


def test_starting_a_run_without_a_connection_is_refused(logged_in: TestClient) -> None:
    response = logged_in.post("/collect/diagnostics", data={"sources": ["tasks"]})
    assert "Configure" in response.headers["location"]


def test_starting_a_run_with_no_source_ticked_is_refused(connected: TestClient) -> None:
    response = connected.post("/collect/diagnostics", data={})
    assert "error" in response.headers["location"]
    assert list_jobs(connected.app.state.db, kind=DIAGNOSTICS_KIND) == []


def test_ticking_sources_queues_a_job_with_exactly_those_sources(
    connected: TestClient,
) -> None:
    app = connected.app  # type: ignore[attr-defined]
    response = connected.post("/collect/diagnostics", data={"sources": ["tasks"]})

    assert response.status_code == 303
    job = list_jobs(app.state.db, kind=DIAGNOSTICS_KIND, limit=1)[0]
    assert job.params["sources"] == ["tasks"]


def test_a_second_run_is_refused_while_one_is_going(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    enqueue(app.state.db, DIAGNOSTICS_KIND, {"sources": ["tasks"]})

    response = connected.post("/collect/diagnostics", data={"sources": ["tasks"]})
    assert "already" in response.headers["location"]


def test_a_finished_run_lists_raw_and_redacted_copies_for_each_source(
    connected: TestClient,
) -> None:
    _collect_diagnostics(
        connected,
        tasks=[{"id": "t1", "end": 1788600000000}],
        backups=[{"id": "b1", "jobName": "Nightly", "status": "success", "end": 1788600000000}],
    )

    body = connected.get("/collect").text
    assert "xapi-tasks.json" in body
    assert "xapi-tasks.redacted.json" in body
    assert "backup-restore-detail.json" in body
    assert "redacted — safe to send" in body
    assert "raw — unmasked" in body


def test_a_failed_run_shows_its_headline_on_the_page(connected: TestClient) -> None:
    """Reported from a real run: the report and archive alone did not answer
    "why did my backup fail" without opening a multi-hundred-KiB JSON file
    and reading it by eye."""
    _collect_diagnostics(
        connected,
        backups=[{"id": "b1", "jobName": "Delta Backup", "status": "failure"}],
        detail={
            "b1": {"message": "backup", "result": {"message": "couldn't instantiate any remote"}}
        },
    )

    body = connected.get("/collect").text
    assert "Delta Backup" in body
    assert "couldn&#39;t instantiate any remote" in body


def test_a_finished_run_shows_the_redaction_report(connected: TestClient) -> None:
    _collect_diagnostics(connected, tasks=[{"id": "t1", "end": 1788600000000}])

    body = connected.get("/collect").text
    assert "IPv4" in body


def test_deleting_a_diagnostics_run_removes_it_from_the_page(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    job_id = _collect_diagnostics(connected, tasks=[{"id": "t1"}])

    response = connected.post(f"/collect/diagnostics/{job_id}/delete")
    assert response.status_code == 303
    assert list_jobs(app.state.db, kind=DIAGNOSTICS_KIND) == []


def test_deleting_something_that_is_not_stored_says_so(connected: TestClient) -> None:
    response = connected.post("/collect/diagnostics/not-a-job/delete")
    assert "error" in response.headers["location"]


def test_downloading_a_stored_diagnostics_artifact_works(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    job_id = _collect_diagnostics(connected, tasks=[{"id": "t1", "end": 1788600000000}])

    artifact = next(
        item for item in list_for_job(app.state.db, job_id) if item.name == "xapi-tasks.json"
    )
    response = connected.get(f"/collect/download/{artifact.id}")

    assert response.status_code == 200
    assert len(response.content) == artifact.size_bytes


def test_a_source_that_cannot_be_read_is_still_shown_as_succeeded_overall(
    connected: TestClient,
) -> None:
    """One source failing must not fail the whole run — mirrors findings.py's
    posture, per the module docstring."""
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/collect/diagnostics", data={"sources": ["tasks", "messages_alarms"]})

    client = _FakeClient()
    client.tasks = lambda since: (_ for _ in ()).throw(XoError("no permission"))
    with patch("app.job_diagnostics.build_client", return_value=client):
        run_pending_jobs(app)

    job = list_jobs(app.state.db, kind=DIAGNOSTICS_KIND, limit=1)[0]
    assert job.state == "succeeded"
    body = connected.get("/collect").text
    assert "messages-alarms.json" in body
