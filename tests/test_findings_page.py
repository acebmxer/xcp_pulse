"""The Findings page: what it renders, and what it refuses.

Reading the rendered HTML rather than the route's inputs, because that is where
the bugs of this kind live: a condition inverted in a template renders a
warning for everybody while every unit test passes. Where a message is
conditional, both sides are asserted — a test that only checks the case where
something should appear passes equally well against something stuck on.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.findings import CRITICAL, INFO, WARNING, Finding, Report, SourceResult
from app.job_findings import KIND
from app.jobs import enqueue
from app.xo_connection import save_connection
from tests.conftest import TEST_PASSWORD, TEST_USER
from tests.helpers import run_pending_jobs

REPORT = Report(
    findings=[
        Finding(
            severity=CRITICAL,
            title="A storage repository backend failed",
            evidence="nfs mount from [IPV4] failed",
            action="Check the SR is attached.",
            source="messages",
            at=1788600000.0,
            count=3,
        ),
        Finding(
            severity=WARNING,
            title="Task failed: API call: vm.delete",
            evidence="VM_BAD_POWER_STATE",
            action="Repeat the operation and watch the task.",
            source="tasks",
            at=1788600000.0,
        ),
    ],
    sources=[
        SourceResult("messages", read=True, examined=1234),
        SourceResult("tasks", read=True, examined=76),
    ],
    window_days=30,
    created_at=1788700000.0,
)


def _connect(client: TestClient) -> None:
    save_connection(
        client.app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=client.app.state.settings.secret_key,
    )


def _store(client: TestClient, report: Report = REPORT) -> None:
    """Run a findings job whose collection is stubbed, so a report is stored."""
    _connect(client)
    enqueue(client.app.state.db, KIND)
    with (
        patch("app.job_findings.build_client"),
        patch("app.job_findings.collect_findings", return_value=report),
    ):
        run_pending_jobs(client.app)


def test_the_page_requires_a_login(client: TestClient) -> None:
    response = client.get("/findings")

    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_starting_a_run_requires_a_login(client: TestClient) -> None:
    response = client.post("/findings")

    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_the_nav_links_to_findings(logged_in: TestClient) -> None:
    assert 'href="/findings"' in logged_in.get("/").text


def test_with_nothing_run_the_page_says_so_rather_than_showing_an_empty_list(
    logged_in: TestClient,
) -> None:
    body = logged_in.get("/findings").text

    assert "Nothing has been read yet" in body
    assert "A storage repository backend failed" not in body


def test_without_a_connection_the_button_is_disabled_and_says_why(
    logged_in: TestClient,
) -> None:
    body = logged_in.get("/findings").text

    assert "No Xen Orchestra connection is configured" in body
    assert "disabled" in body


def test_with_a_connection_the_button_is_offered(logged_in: TestClient) -> None:
    """The other side of the test above."""
    _connect(logged_in)
    body = logged_in.get("/findings").text

    assert "No Xen Orchestra connection is configured" not in body
    assert "Run now" in body


def test_a_run_cannot_be_started_without_a_connection(logged_in: TestClient) -> None:
    response = logged_in.post("/findings")

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_a_run_is_queued_when_a_connection_exists(logged_in: TestClient) -> None:
    _connect(logged_in)
    response = logged_in.post("/findings")

    assert response.status_code == 303
    assert "notice=" in response.headers["location"]


def test_a_second_run_is_refused_while_one_is_going(logged_in: TestClient) -> None:
    """Two reports of the same pool seconds apart say the same thing twice."""
    _connect(logged_in)
    enqueue(logged_in.app.state.db, KIND)

    response = logged_in.post("/findings")

    assert "already" in response.headers["location"]


def test_the_stored_findings_render_with_evidence_and_action(
    logged_in: TestClient,
) -> None:
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "A storage repository backend failed" in body
    assert "nfs mount from [IPV4] failed" in body
    assert "Check the SR is attached." in body
    assert "Task failed: API call: vm.delete" in body


def test_severity_counts_include_the_zeros(logged_in: TestClient) -> None:
    """ "No critical findings" is the answer someone opens the page for."""
    _store(logged_in)
    body = logged_in.get("/findings").text

    for severity in ("critical", "warning", "info"):
        assert f"sev-{severity}" in body


def test_a_repeated_finding_shows_its_count(logged_in: TestClient) -> None:
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "3×" in body


def test_a_clean_report_says_nothing_to_report(logged_in: TestClient) -> None:
    _store(logged_in, Report(sources=[SourceResult("messages", read=True)], window_days=30))
    body = logged_in.get("/findings").text

    assert "Nothing to report" in body
    assert "Nothing has been read yet" not in body


def test_a_refused_source_is_named_above_the_findings(logged_in: TestClient) -> None:
    """A report missing its dashboard has not checked patches or host state.

    That has to be known before the list below it is read as complete, so it
    renders as an alert rather than a row at the bottom.
    """
    _store(
        logged_in,
        Report(
            findings=[Finding(INFO, "Something", "e", "a", "messages")],
            sources=[
                SourceResult("messages", read=True, examined=5),
                SourceResult("dashboard", read=False, reason="needs an administrator account"),
            ],
        ),
    )
    body = logged_in.get("/findings").text

    assert "could not be read" in body
    assert "needs an administrator account" in body


def test_a_fully_read_report_shows_no_unread_warning(logged_in: TestClient) -> None:
    """The other side: the warning must not be stuck on for everybody."""
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "could not be read" not in body


def test_the_sources_table_says_how_much_each_source_held(
    logged_in: TestClient,
) -> None:
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "1,234" in body
    assert "XAPI messages" in body


def test_both_stored_copies_are_offered_for_download(logged_in: TestClient) -> None:
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "findings.json" in body
    assert "findings.md" in body
    assert "/findings/download/" in body


def test_a_stored_report_can_be_downloaded(logged_in: TestClient) -> None:
    from app.artifacts import list_for_job
    from app.jobs import latest_successful

    _store(logged_in)
    job = latest_successful(logged_in.app.state.db, KIND)
    artifact = next(
        item for item in list_for_job(logged_in.app.state.db, job.id) if item.name.endswith(".md")
    )

    response = logged_in.get(f"/findings/download/{artifact.id}")

    assert response.status_code == 200
    assert "XCP Pulse" in response.text


def test_downloading_something_that_does_not_exist_redirects_rather_than_500s(
    logged_in: TestClient,
) -> None:
    response = logged_in.get("/findings/download/does-not-exist")

    assert response.status_code == 303
    assert "/findings" in response.headers["location"]


def test_the_page_refreshes_itself_only_while_a_run_is_going(
    logged_in: TestClient,
) -> None:
    _connect(logged_in)
    assert 'http-equiv="refresh"' not in logged_in.get("/findings").text

    enqueue(logged_in.app.state.db, KIND)
    assert 'http-equiv="refresh"' in logged_in.get("/findings").text


def test_the_page_does_not_call_xen_orchestra(logged_in: TestClient) -> None:
    """It renders the stored artifact, so it loads with XO unreachable.

    The dashboard follows the same rule: the last thing known beats an error
    where the findings were.
    """
    _store(logged_in)

    with patch("app.xo_connection.build_client") as build:
        response = logged_in.get("/findings")

    assert response.status_code == 200
    build.assert_not_called()


def test_the_login_page_is_still_reachable(client: TestClient) -> None:
    """A guard against a route ordering mistake taking out authentication."""
    assert client.get("/login").status_code == 200
    assert (
        client.post(
            "/login",
            data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/findings"},
        ).status_code
        == 303
    )


def test_the_page_warns_when_redaction_rules_were_switched_off(
    logged_in: TestClient,
) -> None:
    """A partly-masked report is the failure worth seeing before it is sent."""
    _store(
        logged_in,
        Report(
            findings=[Finding(WARNING, "Something", "e", "a", "tasks")],
            sources=[SourceResult("tasks", read=True, examined=1)],
            rules_disabled=["UUIDs", "IPv4 addresses"],
        ),
    )
    body = logged_in.get("/findings").text

    assert "2 redaction rules" in body
    assert "were switched off" in body
    assert "UUIDs, IPv4 addresses" in body


def test_the_page_shows_no_masking_warning_when_every_rule_was_on(
    logged_in: TestClient,
) -> None:
    """The other side: the warning must not render for everybody."""
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "switched off" not in body


def test_one_switched_off_rule_reads_as_singular(logged_in: TestClient) -> None:
    _store(
        logged_in,
        Report(
            sources=[SourceResult("tasks", read=True)],
            rules_disabled=["UUIDs"],
        ),
    )
    body = logged_in.get("/findings").text

    assert "1 redaction rule was switched off" in body


def test_the_sources_table_says_where_each_source_comes_from(
    logged_in: TestClient,
) -> None:
    """Naming the route alone does not say whether to look at a host or at XO."""
    _store(logged_in)
    body = logged_in.get("/findings").text

    assert "Comes from" in body
    assert "XCP-ng hosts" in body
    assert "Xen Orchestra" in body
    assert "the hosts&#39; own event record" in body


def test_a_source_that_found_nothing_says_it_was_checked(
    logged_in: TestClient,
) -> None:
    """A bare 0 reads as "nothing examined", which is the wrong fact."""
    _store(
        logged_in,
        Report(
            sources=[SourceResult("alarms", read=True, examined=0)],
            window_days=30,
        ),
    )
    body = logged_in.get("/findings").text

    assert "checked - no alarms" in body


def test_a_source_that_could_not_be_read_says_not_read_in_the_table(
    logged_in: TestClient,
) -> None:
    """The other side: a refusal must not read as a clean zero."""
    _store(
        logged_in,
        Report(
            sources=[SourceResult("dashboard", read=False, reason="needs an administrator")],
            window_days=30,
        ),
    )
    body = logged_in.get("/findings").text

    assert "not read — needs an administrator" in body
    assert "checked - no" not in body
