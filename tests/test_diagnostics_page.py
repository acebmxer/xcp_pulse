"""The Diagnostics page: what it renders, and what it refuses.

Reading the rendered HTML rather than the route's inputs, per the same rule
``test_findings_page.py`` follows: a condition inverted in a template renders
wrong for everybody while every unit test passes, so a warning that should be
conditional is checked on both sides.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.job_api_diagnostics import KIND
from app.jobs import enqueue
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


def _connect(client: TestClient) -> None:
    save_connection(
        client.app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=client.app.state.settings.secret_key,
    )


def _store(client: TestClient, **records) -> None:
    """Run a diagnostics job against a stubbed client, so a report is stored."""
    _connect(client)
    enqueue(client.app.state.db, KIND)
    with patch("app.job_api_diagnostics.build_client", return_value=_FakeClient(**records)):
        run_pending_jobs(client.app)


def test_the_page_requires_a_login(client: TestClient) -> None:
    response = client.get("/diagnostics")

    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_starting_a_run_requires_a_login(client: TestClient) -> None:
    response = client.post("/diagnostics")

    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_the_nav_links_to_diagnostics(logged_in: TestClient) -> None:
    assert 'href="/diagnostics"' in logged_in.get("/").text


def test_with_nothing_run_the_page_says_so(logged_in: TestClient) -> None:
    body = logged_in.get("/diagnostics").text

    assert "Nothing has been collected yet" in body


def test_without_a_connection_the_button_is_disabled_and_says_why(
    logged_in: TestClient,
) -> None:
    body = logged_in.get("/diagnostics").text

    assert "No Xen Orchestra connection is configured" in body
    assert "disabled" in body


def test_with_a_connection_the_button_is_offered(logged_in: TestClient) -> None:
    _connect(logged_in)
    body = logged_in.get("/diagnostics").text

    assert "No Xen Orchestra connection is configured" not in body
    assert "Run now" in body


def test_a_run_cannot_be_started_without_a_connection(logged_in: TestClient) -> None:
    response = logged_in.post("/diagnostics")

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_a_run_is_queued_when_a_connection_exists(logged_in: TestClient) -> None:
    _connect(logged_in)
    response = logged_in.post("/diagnostics")

    assert response.status_code == 303
    assert "notice=" in response.headers["location"]


def test_a_second_run_is_refused_while_one_is_going(logged_in: TestClient) -> None:
    _connect(logged_in)
    enqueue(logged_in.app.state.db, KIND)

    response = logged_in.post("/diagnostics")

    assert "already" in response.headers["location"]


def test_the_sources_table_shows_what_was_read(logged_in: TestClient) -> None:
    _store(
        logged_in,
        backups=[{"id": "b1", "jobName": "Nightly", "status": "success"}],
        tasks=[{"id": "t1"}],
    )
    body = logged_in.get("/diagnostics").text

    assert "Backup runs" in body
    assert "XAPI tasks" in body
    assert "1 record(s)" in body


def test_a_failed_backup_run_and_its_detail_are_shown(logged_in: TestClient) -> None:
    _store(
        logged_in,
        backups=[
            {"id": "b1", "jobName": "Nightly", "status": "failure"},
            {"id": "b2", "jobName": "Weekly", "status": "success"},
        ],
        detail={"b1": {"tasks": ["snapshot failed"]}},
    )
    body = logged_in.get("/diagnostics").text

    assert "Nightly" in body
    assert "snapshot failed" in body
    assert "Weekly" not in body


def test_the_failure_headline_is_pulled_out_above_the_raw_detail(
    logged_in: TestClient,
) -> None:
    """A real failure's ``result.message`` is the one line worth reading
    first; the rest is the raw tree, no longer a Python repr."""
    _store(
        logged_in,
        backups=[{"id": "b1", "jobName": "Delta Backup", "status": "failure"}],
        detail={
            "b1": {"message": "backup", "result": {"message": "couldn't instantiate any remote"}}
        },
    )
    body = logged_in.get("/diagnostics").text

    assert "<strong>couldn&#39;t instantiate any remote</strong>" in body
    assert "{&#39;message&#39;:" not in body


def test_both_stored_copies_are_offered_for_download(logged_in: TestClient) -> None:
    _store(logged_in)
    body = logged_in.get("/diagnostics").text

    assert "diagnostics.json" in body
    assert "diagnostics.md" in body
    assert "/diagnostics/download/" in body


def test_downloading_something_that_does_not_exist_redirects_rather_than_500s(
    logged_in: TestClient,
) -> None:
    response = logged_in.get("/diagnostics/download/does-not-exist")

    assert response.status_code == 303
    assert "/diagnostics" in response.headers["location"]


def test_the_page_warns_when_redaction_rules_were_switched_off(
    logged_in: TestClient,
) -> None:
    from app.redact import set_enabled_rules

    set_enabled_rules(logged_in.app.state.db, {"mac"})
    _store(logged_in)
    body = logged_in.get("/diagnostics").text

    assert "were switched off" in body


def test_the_page_shows_no_masking_warning_when_every_rule_was_on(
    logged_in: TestClient,
) -> None:
    _store(logged_in)
    body = logged_in.get("/diagnostics").text

    assert "switched off" not in body


def test_the_page_does_not_call_xen_orchestra(logged_in: TestClient) -> None:
    """It renders the stored artifact, so it loads with XO unreachable."""
    _store(logged_in)

    with patch("app.xo_connection.build_client") as build:
        response = logged_in.get("/diagnostics")

    assert response.status_code == 200
    build.assert_not_called()


def test_the_page_refreshes_itself_only_while_a_run_is_going(
    logged_in: TestClient,
) -> None:
    _connect(logged_in)
    assert 'http-equiv="refresh"' not in logged_in.get("/diagnostics").text

    enqueue(logged_in.app.state.db, KIND)
    assert 'http-equiv="refresh"' in logged_in.get("/diagnostics").text
