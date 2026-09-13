"""The jobs page: starting, watching and cancelling work from the browser."""

from __future__ import annotations

import re
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.artifacts import artifact_path, list_for_job
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
    """Queueing another identical refresh produces nothing the first will not.

    The pending job is enqueued directly rather than through the route, and the
    app's worker thread is stopped first, so it stays queued for the second
    request to be refused against. Queueing the first through the route let the
    worker claim and fail it — against the fixture's unreachable
    ``xo.example.com`` — before the second request arrived, leaving nothing
    active and the guard measuring timing rather than behaviour. That failed on
    CI under Python 3.12 while passing under 3.13 on the same commit.
    """
    app = connected.app  # type: ignore[attr-defined]
    worker_thread = getattr(app.state, "job_worker", None)
    if worker_thread is not None:
        worker_thread.stop()

    enqueue(app.state.db, INVENTORY_KIND)
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


def test_the_refresh_tag_is_actually_inside_head(connected: TestClient) -> None:
    """A <meta> tag outside <head> is invalid HTML and browsers ignore it —
    which is exactly how this tag sat for a while: rendered into
    {% block content %} (the page body), present in the response text so a
    plain substring check like the test above never noticed, but inert in a
    real browser. head_extra is the block base.html defines inside <head> for
    this; assert the tag actually lands there, not merely that it's present
    somewhere in the page.
    """
    app = connected.app  # type: ignore[attr-defined]
    enqueue(app.state.db, INVENTORY_KIND)
    body = connected.get("/jobs").text

    head = re.search(r"<head>(.*?)</head>", body, re.DOTALL)
    assert head is not None
    assert 'http-equiv="refresh"' in head.group(1)


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


def test_every_stored_file_has_a_download_link(connected: TestClient) -> None:
    """A file the page names but cannot hand over is a result nobody can use.

    Both artifacts a redaction produces are checked, because the redacted copy
    is the point of the run and the report is what says what it masked.
    """
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/jobs/redact", data={"artifact_id": _inventory_artifact(app)})
    run_pending_jobs(app)

    body = connected.get("/jobs").text
    job = list_jobs(app.state.db, kind=REDACT_KIND)[0]
    produced = list_for_job(app.state.db, job.id)

    assert len(produced) == 2
    for artifact in produced:
        assert f'href="/jobs/download/{artifact.id}"' in body


def test_downloading_a_stored_file_serves_its_bytes(connected: TestClient) -> None:
    """The name the browser saves under is the artifact's, not the uuid on disk."""
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/jobs/redact", data={"artifact_id": _inventory_artifact(app)})
    run_pending_jobs(app)

    job = list_jobs(app.state.db, kind=REDACT_KIND)[0]
    report = next(
        item for item in list_for_job(app.state.db, job.id) if item.name == "redaction-report.json"
    )

    response = connected.get(f"/jobs/download/{report.id}")
    assert response.status_code == 200
    assert "redaction-report.json" in response.headers["content-disposition"]
    assert response.json()["source"]["name"] == "inventory.json"


def test_downloading_a_file_that_is_gone_says_so(connected: TestClient) -> None:
    response = connected.get("/jobs/download/no-such-artifact", follow_redirects=False)

    assert response.status_code == 303
    assert "no+longer+stored" in response.headers["location"]


def test_deleting_a_redaction_removes_its_files_and_its_row(connected: TestClient) -> None:
    """The copy and the report go together — half a redaction is not a result."""
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/jobs/redact", data={"artifact_id": _inventory_artifact(app)})
    run_pending_jobs(app)

    job = list_jobs(app.state.db, kind=REDACT_KIND)[0]
    paths = [
        artifact_path(app.state.settings.data_dir, job.id, item.id)
        for item in list_for_job(app.state.db, job.id)
    ]
    assert paths and all(path.is_file() for path in paths)

    response = connected.post(f"/jobs/{job.id}/delete")

    assert "Redaction+deleted" in response.headers["location"]
    assert list_jobs(app.state.db, kind=REDACT_KIND) == []
    assert list_for_job(app.state.db, job.id) == []
    assert not any(path.exists() for path in paths)


def test_the_delete_button_shows_only_on_redactions(connected: TestClient) -> None:
    """A collection is deleted from the Collect page, where its size is shown."""
    app = connected.app  # type: ignore[attr-defined]
    _inventory_artifact(app)
    inventory_job = list_jobs(app.state.db, kind=INVENTORY_KIND)[0]

    body = connected.get("/jobs").text
    assert f'action="/jobs/{inventory_job.id}/delete"' not in body

    connected.post("/jobs/redact", data={"artifact_id": _inventory_artifact(app)})
    run_pending_jobs(app)
    redact_job = list_jobs(app.state.db, kind=REDACT_KIND)[0]

    body = connected.get("/jobs").text
    assert f'action="/jobs/{redact_job.id}/delete"' in body
    assert f'action="/jobs/{inventory_job.id}/delete"' not in body


def test_deleting_a_job_that_is_not_a_redaction_is_refused(connected: TestClient) -> None:
    """The route must not become a way to delete a 2.3 GiB collection unseen."""
    app = connected.app  # type: ignore[attr-defined]
    _inventory_artifact(app)
    job = list_jobs(app.state.db, kind=INVENTORY_KIND)[0]

    response = connected.post(f"/jobs/{job.id}/delete")

    assert "no+such+redaction" in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=INVENTORY_KIND)) == 1


def test_redacting_the_same_file_with_the_same_rules_is_refused(connected: TestClient) -> None:
    """A second identical run costs a second copy and answers nothing new."""
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    response = connected.post("/jobs/redact", data={"artifact_id": artifact_id})

    assert "already+redacted" in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=REDACT_KIND)) == 1


def test_redacting_the_same_file_with_different_rules_is_allowed(connected: TestClient) -> None:
    """A different set of rules is a different result, which is the point.

    The negative half of the guard above: a check that only refuses proves
    nothing about a condition that could be stuck on.
    """
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    set_enabled_rules(app.state.db, [rule.name for rule in RULES if rule.name != "uuid"])
    response = connected.post("/jobs/redact", data={"artifact_id": artifact_id})

    assert response.status_code == 303
    assert "already+redacted" not in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=REDACT_KIND)) == 2


def test_a_different_file_with_the_same_rules_is_allowed(connected: TestClient) -> None:
    """The guard is per file, not a global "one redaction with these rules"."""
    app = connected.app  # type: ignore[attr-defined]
    first = _inventory_artifact(app)
    connected.post("/jobs/redact", data={"artifact_id": first})
    run_pending_jobs(app)

    second = _inventory_artifact(app)
    response = connected.post("/jobs/redact", data={"artifact_id": second})

    assert response.status_code == 303
    assert "already+redacted" not in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=REDACT_KIND)) == 2


def test_redacting_again_is_allowed_once_the_first_result_is_deleted(
    connected: TestClient,
) -> None:
    """The refusal points at a stored result; with none stored there is none."""
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    job = list_jobs(app.state.db, kind=REDACT_KIND)[0]
    connected.post(f"/jobs/{job.id}/delete")

    response = connected.post("/jobs/redact", data={"artifact_id": artifact_id})

    assert response.status_code == 303
    assert "already+redacted" not in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=REDACT_KIND)) == 1


def test_the_refusal_names_the_redaction_that_already_answers_it(
    connected: TestClient,
) -> None:
    """A refusal naming nothing sends the operator to scroll the history.

    Checked on the rendered banner rather than the redirect, because what
    matters is the sentence the operator reads.
    """
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    set_enabled_rules(app.state.db, [rule.name for rule in RULES if rule.name != "uuid"])
    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    body = connected.post(
        "/jobs/redact", data={"artifact_id": artifact_id}, follow_redirects=True
    ).text
    banner = " ".join(re.search(r'alert-error">(.*?)</div>', body, re.S).group(1).split())

    assert "already redacted just now" in banner
    # Which rule was off, so the operator can tell what a rerun would change.
    assert "UUIDs switched off" in banner
    assert "delete that redaction" in banner


def test_the_refusal_says_when_every_rule_was_on(connected: TestClient) -> None:
    """The all-on case has no rule to name, and must not read as a bug."""
    app = connected.app  # type: ignore[attr-defined]
    artifact_id = _inventory_artifact(app)
    connected.post("/jobs/redact", data={"artifact_id": artifact_id})
    run_pending_jobs(app)

    body = connected.post(
        "/jobs/redact", data={"artifact_id": artifact_id}, follow_redirects=True
    ).text
    banner = " ".join(re.search(r'alert-error">(.*?)</div>', body, re.S).group(1).split())

    assert "with every rule switched on" in banner


def test_hit_counts_are_thousands_separated() -> None:
    """A rule column spanning 12 to 464,679 is unreadable without separators.

    Both ends are asserted: a large count must gain separators, and a small one
    must not gain anything, since a check on only the large case passes just as
    well against a filter that mangles short numbers.
    """
    from app.dependencies import count

    assert count(464679) == "464,679"
    assert count(12) == "12"
    assert count(0) == "0"
    assert count(1000) == "1,000"


def test_stored_progress_lines_are_separated_on_render() -> None:
    """A job's step text is frozen at write time, so it is formatted on render.

    Formatting it in the f-string that writes it would leave every row recorded
    by an earlier version unseparated for good, which is most of the history a
    page shows. Byte sizes must survive untouched: "863.1 MiB" is not an
    integer to separate, and mangling it would misreport a bundle's size.
    """
    from app.dependencies import counts_in

    assert (
        counts_in("xcp-ng-host1: 471729 value(s) masked, 863.1 MiB stored")
        == "xcp-ng-host1: 471,729 value(s) masked, 863.1 MiB stored"
    )
    assert counts_in("756 value(s) masked in 3500461 line(s)") == (
        "756 value(s) masked in 3,500,461 line(s)"
    )
    # Short counts and byte sizes are left exactly as they were.
    assert counts_in("6 value(s) masked in 39 line(s)") == "6 value(s) masked in 39 line(s)"
    assert counts_in("2.3 GiB stored") == "2.3 GiB stored"
    assert counts_in(None) == ""

    # Regression test: a scanned bundle member's own name is progress text
    # too ("Scanning {member.name}", collect_log_findings), and a dated log
    # file like "sa20250911" (a real sysstat file name) was getting split into
    # "sa20,250,911" — a filename is not a count. A digit run glued to a
    # letter on either side is left alone; a bare 4+ digit count next to
    # punctuation still separates.
    assert counts_in("Scanning var/log/sa/sa20250911") == "Scanning var/log/sa/sa20250911"
