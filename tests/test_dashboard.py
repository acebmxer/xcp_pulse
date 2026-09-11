"""The dashboard, and what it shows for each state of the connection.

The states are distinct on purpose: no connection at all, a refresh that has not
finished, a connection that cannot be reached, a connection whose account can
see nothing, and a working one. The empty case is the subtle one — Xen
Orchestra answers an account without privileges with 200 and an empty list, so
"nothing to show" must not be reported as success with an empty pool list.

Since v0.4.0 the page shows what a **Refresh inventory** job stored rather than
calling XO itself, so these run the job and then read the page — which is the
real path, and would not notice if the job stopped storing what the page reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.findings import CRITICAL, Finding, SourceResult
from app.findings import Report as FindingsReport
from app.job_findings import KIND as FINDINGS_KIND
from app.job_inventory import KIND as INVENTORY_KIND
from app.jobs import enqueue, list_jobs
from app.redact import RULES, set_enabled_rules
from app.routes.dashboard import RECENT_JOBS
from app.xo_client import Host, Inventory, Pool, XoError
from app.xo_connection import save_connection
from tests.helpers import run_pending_jobs

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


def _refresh(
    client: TestClient,
    inventory: Inventory | None = None,
    error: Exception | None = None,
) -> None:
    """Run one inventory refresh against a stubbed XO, as the worker would."""
    app = client.app  # type: ignore[attr-defined]
    enqueue(app.state.db, INVENTORY_KIND)
    with patch("app.job_inventory.build_client") as build:
        if error is not None:
            build.side_effect = error
        else:
            build.return_value.inventory.return_value = inventory or Inventory()
        run_pending_jobs(app)


def test_dashboard_without_a_connection_points_at_settings(logged_in: TestClient) -> None:
    body = logged_in.get("/").text
    assert "No Xen Orchestra connection configured" in body
    assert "/settings" in body


def test_dashboard_lists_pools_and_hosts(connected: TestClient) -> None:
    _refresh(connected, Inventory(pools=[POOL], hosts=HOSTS))
    body = connected.get("/").text

    assert "xcp-ng-Pool1" in body
    assert "xcp-ng-host1" in body
    assert "10.100.2.10" in body
    assert "XCP-ng 8.3.0" in body
    assert "28 cores" in body
    assert "37% memory" in body
    assert "No Xen Orchestra connection configured" not in body


def test_dashboard_marks_the_pool_master_and_disabled_hosts(connected: TestClient) -> None:
    _refresh(connected, Inventory(pools=[POOL], hosts=HOSTS))
    body = connected.get("/").text

    assert "master" in body
    assert "disabled" in body


def test_dashboard_explains_an_empty_inventory(connected: TestClient) -> None:
    """An account that can see nothing must not look like an empty pool.

    XO answers both a privilege-less account and an empty installation with
    200 and [], so the page has to offer both causes rather than asserting the
    one it cannot tell apart from the other.
    """
    _refresh(connected, Inventory())
    body = connected.get("/").text

    assert "Nothing visible to this account" in body
    assert "needs pool and host read privileges" in body
    assert "nothing registered in this Xen Orchestra" in body


def test_dashboard_shows_a_connection_failure(connected: TestClient) -> None:
    _refresh(connected, error=XoError("cannot reach https://xo.example.com"))
    response = connected.get("/")

    assert response.status_code == 200, "an unreachable XO must not break the page"
    assert "cannot reach https://xo.example.com" in response.text


def test_a_failed_refresh_keeps_the_last_good_inventory_on_screen(connected: TestClient) -> None:
    """The reason for storing results rather than reading XO on each page load.

    A pool that was visible a minute ago is still the best answer available
    when XO stops responding. Losing it and showing an error where the hosts
    were would be a worse page than a stale one that says it is stale.
    """
    _refresh(connected, Inventory(pools=[POOL], hosts=HOSTS))
    _refresh(connected, error=XoError("cannot reach https://xo.example.com"))

    body = connected.get("/").text
    assert "xcp-ng-host1" in body, "the last good inventory must survive a failed refresh"
    assert "cannot reach https://xo.example.com" in body, "the failure must still be reported"


def test_dashboard_reports_a_changed_secret_key(connected: TestClient) -> None:
    """The token was encrypted under a key that no longer exists.

    The fix is to re-enter the token, which is a different action from fixing
    an unreachable address, so the message names the token rather than the
    address.
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

    enqueue(app.state.db, INVENTORY_KIND)
    run_pending_jobs(app)

    body = connected.get("/").text
    assert "secret key" in body


def test_the_first_load_queues_a_refresh_by_itself(connected: TestClient) -> None:
    """Saving a connection and finding an empty dashboard would be a dead end.

    Nothing is stored until a job has run, so the first load queues one rather
    than requiring the operator to find the jobs page to see anything at all.
    """
    app = connected.app  # type: ignore[attr-defined]
    body = connected.get("/").text

    from app.jobs import list_jobs

    jobs = list_jobs(app.state.db, kind=INVENTORY_KIND)
    assert len(jobs) == 1, "the first load must queue exactly one refresh"
    assert "Reading the inventory" in body


def test_the_dashboard_does_not_queue_a_second_refresh_on_every_load(
    connected: TestClient,
) -> None:
    """Auto-queueing is for the first load only, not a refresh per page view."""
    app = connected.app  # type: ignore[attr-defined]
    connected.get("/")
    run_pending_jobs(app)
    connected.get("/")
    connected.get("/")

    from app.jobs import list_jobs

    assert len(list_jobs(app.state.db, kind=INVENTORY_KIND)) == 1


def test_dashboard_requires_login(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# --- The status panels ---------------------------------------------------
#
# Both sides of every condition are asserted. A panel that renders its warning
# unconditionally passes a test that only checks the case where the warning
# belongs, which is exactly how a stuck-on message reaches a released page.


def _store_findings(client: TestClient, report: FindingsReport) -> None:
    """Run a findings job whose collection is stubbed, so a report is stored."""
    enqueue(client.app.state.db, FINDINGS_KIND)  # type: ignore[attr-defined]
    with (
        patch("app.job_findings.build_client"),
        patch("app.job_findings.collect_findings", return_value=report),
    ):
        run_pending_jobs(client.app)  # type: ignore[attr-defined]


def test_the_panels_are_absent_without_a_connection(logged_in: TestClient) -> None:
    """Nothing to report on until XO is configured, so the panels stay off."""
    body = logged_in.get("/").text

    assert "Recent activity" not in body
    assert 'href="/findings">Open' not in body


def test_the_findings_panel_shows_the_severity_counts(connected: TestClient) -> None:
    _store_findings(
        connected,
        FindingsReport(
            findings=[
                Finding(
                    severity=CRITICAL,
                    title="A storage repository backend failed",
                    evidence="nfs mount from [IPV4] failed",
                    action="Check the SR is attached.",
                    source="messages",
                ),
            ],
            sources=[SourceResult("messages", read=True, examined=799)],
            created_at=1788700000.0,
        ),
    )
    body = connected.get("/").text

    assert "Findings" in body
    assert "sev-critical" in body
    assert "Nothing has been read yet" not in body


def test_the_findings_panel_says_when_nothing_has_run(connected: TestClient) -> None:
    body = connected.get("/").text

    assert "Nothing has been read yet" in body
    assert "sev-critical" not in body


def test_the_findings_panel_reports_an_unread_source(connected: TestClient) -> None:
    """A refused source means the counts above it are not the whole picture."""
    _store_findings(
        connected,
        FindingsReport(
            sources=[
                SourceResult("messages", read=True, examined=799),
                SourceResult("dashboard", read=False, reason="not enough privileges"),
            ],
            created_at=1788700000.0,
        ),
    )
    body = connected.get("/").text

    assert "could not be read" in body


def test_the_findings_panel_is_silent_when_every_source_was_read(
    connected: TestClient,
) -> None:
    _store_findings(
        connected,
        FindingsReport(
            sources=[SourceResult("messages", read=True, examined=799)],
            created_at=1788700000.0,
        ),
    )
    body = connected.get("/").text

    assert "could not be read" not in body


def test_the_redaction_panel_names_the_rules_that_are_off(connected: TestClient) -> None:
    app = connected.app  # type: ignore[attr-defined]
    keep_on = [rule.name for rule in RULES if rule.name != "ipv4"]
    set_enabled_rules(app.state.db, keep_on)

    body = connected.get("/").text

    assert f"1 of {len(RULES)} rules off" in body
    assert "IPv4 addresses" in body
    assert "All 8 rules on" not in body


def test_the_redaction_panel_says_so_when_every_rule_is_on(connected: TestClient) -> None:
    """The other side of the banner above — it must not be stuck on."""
    body = connected.get("/").text

    assert f"All {len(RULES)} rules on" in body
    assert "rules off" not in body


def test_the_storage_panel_reports_an_empty_store(connected: TestClient) -> None:
    body = connected.get("/").text

    assert "No collections stored yet" in body


def test_the_recent_activity_panel_lists_jobs(connected: TestClient) -> None:
    _refresh(connected, Inventory(pools=[POOL], hosts=HOSTS))
    body = connected.get("/").text

    assert "Recent activity" in body
    assert "refresh inventory" in body
    assert "succeeded" in body
    assert "Nothing has run yet" not in body


def test_the_recent_activity_panel_shows_a_failure(connected: TestClient) -> None:
    """A failed job is what makes every other figure on the page stale, which
    is the reason this panel is here at all."""
    _refresh(connected, error=XoError("cannot reach https://xo.example.com"))
    body = connected.get("/").text

    assert "failed" in body
    assert "Nothing has run yet" not in body


def test_the_activity_panel_shows_at_most_the_recent_few(connected: TestClient) -> None:
    """It answers "did the last thing work?", not "what has ever run" — the
    jobs page is where a history is read."""
    app = connected.app  # type: ignore[attr-defined]
    for _ in range(RECENT_JOBS + 3):
        _refresh(connected, Inventory(pools=[POOL], hosts=HOSTS))

    assert len(list_jobs(app.state.db)) > RECENT_JOBS
    body = connected.get("/").text
    assert body.count('class="job-state job-state-') == RECENT_JOBS
