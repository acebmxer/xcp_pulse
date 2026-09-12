"""The Collect page: starting a collection, downloading it, and cleaning up.

The download route is the point of the application, so most of what is checked
here is that the right bytes come back with the right name — and that the page
says which copy is the redacted one, because sending the wrong file is the
mistake this whole feature exists to prevent.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.artifacts import list_for_job, store_json
from app.job_collect import KIND as COLLECT_KIND
from app.job_extract import KIND as EXTRACT_KIND
from app.job_inventory import KIND as INVENTORY_KIND
from app.jobs import enqueue, list_jobs, mark_succeeded
from app.xo_client import Host, Inventory, Pool
from app.xo_connection import save_connection
from tests.helpers import run_pending_jobs

HOST = Host(
    id="host-1",
    name="xcp-ng-host1",
    address="10.100.2.10",
    version="8.3.0",
    product="XCP-ng",
    power_state="Running",
    pool_id="pool-1",
)
POOL = Pool(id="pool-1", name="Pool1", master_id="host-1")

LOG_LINE = "Sep  6 12:30:46 xen01 xapi: host at 10.20.30.41\n"


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


@pytest.fixture
def with_inventory(connected: TestClient) -> Iterator[TestClient]:
    """A stored inventory, so the page has a host to offer."""
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/jobs/refresh-inventory")
    with patch("app.job_inventory.build_client") as build:
        build.return_value.inventory.return_value = Inventory(pools=[POOL], hosts=[HOST])
        run_pending_jobs(app)
    yield connected


def _bundle() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        body = LOG_LINE.encode("utf-8")
        info = tarfile.TarInfo("var/log/xensource.log")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


class _FakeClient:
    def download_logs(self, host_id, destination, *, on_chunk=None):
        payload = _bundle()
        destination.write_bytes(payload)
        return len(payload)

    def download_audit(self, host_id, destination, *, on_chunk=None):
        destination.write_bytes(b"audit from 10.20.30.77\n")
        return 23


def _collect(client: TestClient) -> str:
    """Start and run a collection, returning its job id.

    Sends ``redact=1`` explicitly, the same value the page's own checkbox
    submits by default (pre-ticked) — this helper is used by tests about
    other parts of a collection, which still expect the redacted copy the
    real form produces unless an operator unticks the box.
    """
    app = client.app  # type: ignore[attr-defined]
    client.post("/collect", data={"host_id": HOST.id, "redact": "1"})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)
    return list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0].id


def test_the_collect_page_requires_login(client: TestClient) -> None:
    assert client.get("/collect").status_code == 303


def test_without_a_connection_the_page_says_where_to_add_one(logged_in: TestClient) -> None:
    body = logged_in.get("/collect").text
    assert "No Xen Orchestra connection is configured" in body


def test_without_an_inventory_the_page_says_to_refresh_first(connected: TestClient) -> None:
    body = connected.get("/collect").text
    assert "No hosts are known yet" in body


def test_the_page_offers_every_known_host(with_inventory: TestClient) -> None:
    body = with_inventory.get("/collect").text
    assert HOST.name in body
    assert HOST.address in body


def test_starting_a_collection_queues_a_job_for_that_host(with_inventory: TestClient) -> None:
    """A bare post, with no checkboxes ticked at all — the same shape a
    browser sends when every box on the form is unticked."""
    app = with_inventory.app  # type: ignore[attr-defined]

    response = with_inventory.post("/collect", data={"host_id": HOST.id})
    assert response.status_code == 303

    job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    assert job.params == {
        "host_id": HOST.id,
        "host_name": HOST.name,
        "include_audit": False,
        "redact": False,
    }


def test_ticking_the_audit_box_asks_the_job_for_the_trail(
    with_inventory: TestClient,
) -> None:
    """A browser sends the checkbox only when it is ticked.

    The unticked case is covered by the test above, which is what proves the
    box is a choice rather than a label on something that always happens.
    """
    app = with_inventory.app  # type: ignore[attr-defined]

    with_inventory.post("/collect", data={"host_id": HOST.id, "include_audit": "1"})

    job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    assert job.params["include_audit"] is True


def test_the_page_offers_the_audit_trail_as_an_unticked_choice(
    with_inventory: TestClient,
) -> None:
    body = with_inventory.get("/collect").text
    assert 'name="include_audit"' in body
    assert "checked" not in body.split('name="include_audit"')[1].split(">")[0]


def test_the_page_offers_redaction_as_a_ticked_choice(with_inventory: TestClient) -> None:
    """The opposite default from the audit checkbox: this one ships on, since
    an unmasked bundle sitting on disk is the surprise to guard against."""
    body = with_inventory.get("/collect").text
    assert 'name="redact"' in body
    assert "checked" in body.split('name="redact"')[1].split(">")[0]


def test_unticking_redact_stores_only_the_raw_bundle(with_inventory: TestClient) -> None:
    """A browser omits an unticked checkbox entirely, same as the audit one."""
    app = with_inventory.app  # type: ignore[attr-defined]

    with_inventory.post("/collect", data={"host_id": HOST.id})

    job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    assert job.params["redact"] is False


def test_a_host_that_is_not_in_the_inventory_is_refused(with_inventory: TestClient) -> None:
    """The id comes from a form, so it is not to be trusted as a host."""
    app = with_inventory.app  # type: ignore[attr-defined]

    response = with_inventory.post("/collect", data={"host_id": "not-a-host"})
    assert response.status_code == 303
    assert "error" in response.headers["location"]
    assert list_jobs(app.state.db, kind=COLLECT_KIND) == []


def test_a_second_collection_is_refused_while_one_is_running(
    with_inventory: TestClient,
) -> None:
    """Two 433 MB downloads compete for one disk and one XO for no gain."""
    app = with_inventory.app  # type: ignore[attr-defined]

    with_inventory.post("/collect", data={"host_id": HOST.id})
    response = with_inventory.post("/collect", data={"host_id": HOST.id})

    assert "already+running" in response.headers["location"]
    assert len(list_jobs(app.state.db, kind=COLLECT_KIND)) == 1


def test_collecting_without_a_connection_is_refused(logged_in: TestClient) -> None:
    response = logged_in.post("/collect", data={"host_id": HOST.id})
    assert "Configure" in response.headers["location"]


def test_the_page_marks_which_copy_is_redacted_and_which_is_raw(
    with_inventory: TestClient,
) -> None:
    """Sending the wrong file is the mistake this whole feature prevents."""
    _collect(with_inventory)

    body = with_inventory.get("/collect").text
    assert "redacted — safe to send" in body
    assert "raw — unmasked" in body


def test_a_raw_only_collection_offers_a_redact_now_button(
    with_inventory: TestClient,
) -> None:
    """The card for a collection stored with redaction switched off names the
    raw bundle as unmasked and offers to redact it, in place of the report."""
    app = with_inventory.app  # type: ignore[attr-defined]
    with_inventory.post("/collect", data={"host_id": HOST.id})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)

    body = with_inventory.get("/collect").text
    assert "Not redacted yet" in body
    assert "Redact now" in body
    assert "redacted — safe to send" not in body


def test_redact_now_updates_the_card_once_it_finishes(
    with_inventory: TestClient,
) -> None:
    """Pressing "Redact now" on a raw-only collection's card must change what
    that same card shows, not leave it reading "not redacted" forever.

    Reported from a real run: "Redact now" posts to the Jobs page's own
    ``/jobs/redact`` route, which redacts via a *separate*
    ``redact_artifact`` job — so the redacted copy and report are stored
    under that job's id, never the collection's. The Collect page's report
    and file-list lookups only ever read the collection's own id, so the
    warning and "Redact now" button kept showing after a real redaction had
    already succeeded.
    """
    app = with_inventory.app  # type: ignore[attr-defined]
    with_inventory.post("/collect", data={"host_id": HOST.id})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)

    collect_job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    raw_bundle = next(
        item for item in list_for_job(app.state.db, collect_job.id) if item.name.endswith(".tgz")
    )

    response = with_inventory.post("/jobs/redact", data={"artifact_id": raw_bundle.id})
    assert response.status_code == 303
    run_pending_jobs(app)

    body = with_inventory.get("/collect").text
    assert "Not redacted yet" not in body
    assert "redacted — safe to send" in body
    assert "IPv4" in body


def test_the_page_shows_the_redaction_report_for_a_collection(
    with_inventory: TestClient,
) -> None:
    _collect(with_inventory)

    body = with_inventory.get("/collect").text
    assert "IPv4" in body


def test_downloading_a_redacted_bundle_returns_the_stored_bytes(
    with_inventory: TestClient,
) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    artifact = next(
        item for item in list_for_job(app.state.db, job_id) if ".redacted." in item.name
    )
    response = with_inventory.get(f"/collect/download/{artifact.id}")

    assert response.status_code == 200
    assert len(response.content) == artifact.size_bytes
    assert artifact.name in response.headers["content-disposition"]


def test_a_downloaded_redacted_bundle_opens_as_a_tarball_without_the_address(
    with_inventory: TestClient, tmp_path
) -> None:
    """End to end: what reaches the browser is a usable, masked archive."""
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    artifact = next(
        item
        for item in list_for_job(app.state.db, job_id)
        if item.name.endswith("-logs.redacted.tgz")
    )
    response = with_inventory.get(f"/collect/download/{artifact.id}")

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:*") as archive:
        text = archive.extractfile("var/log/xensource.log").read().decode("utf-8")
    assert "10.20.30.41" not in text
    assert "[IPv4]" in text


def test_downloading_something_that_is_not_stored_says_so(logged_in: TestClient) -> None:
    response = logged_in.get("/collect/download/no-such-artifact")
    assert response.status_code == 303
    assert "no+longer+stored" in response.headers["location"]


def test_downloading_requires_login(client: TestClient) -> None:
    assert client.get("/collect/download/anything").status_code == 303


def test_deleting_a_collection_removes_it_from_the_page(
    with_inventory: TestClient,
) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    response = with_inventory.post(f"/collect/{job_id}/delete")
    assert response.status_code == 303
    assert list_jobs(app.state.db, kind=COLLECT_KIND) == []


def test_the_retention_preview_names_what_would_go(logged_in: TestClient) -> None:
    """The preview has to appear before the button that acts on it."""
    app = logged_in.app  # type: ignore[attr-defined]
    db = app.state.db

    job = enqueue(db, COLLECT_KIND, {"host_id": "old", "host_name": "old-host"})
    store_json(db, app.state.settings.data_dir, job_id=job.id, name="old-logs.tgz", payload={})
    mark_succeeded(db, job.id)
    db.execute("UPDATE jobs SET finished_at = 0, created_at = 0 WHERE id = ?", (job.id,))
    db.commit()

    body = logged_in.get("/collect?keep_days=30&keep_count=0").text
    # Whitespace-insensitive: the wording spans lines in the template.
    assert "This would delete 1 collection," in " ".join(body.split())
    assert "old-host" in body


def test_the_preview_pluralises_the_collection_count(logged_in: TestClient) -> None:
    """Reads "1 collection", not "1 collection(s)" — an operator reads this page."""
    app = logged_in.app  # type: ignore[attr-defined]
    db = app.state.db

    for name in ("old-one", "old-two"):
        job = enqueue(db, COLLECT_KIND, {"host_id": name, "host_name": name})
        store_json(db, app.state.settings.data_dir, job_id=job.id, name="logs.tgz", payload={})
        mark_succeeded(db, job.id)
        db.execute("UPDATE jobs SET finished_at = 0, created_at = 0 WHERE id = ?", (job.id,))
    db.commit()

    body = " ".join(logged_in.get("/collect?keep_days=30&keep_count=0").text.split())
    assert "This would delete 2 collections," in body
    assert "collection(s)" not in body


def test_the_preview_says_when_nothing_would_be_deleted(logged_in: TestClient) -> None:
    body = logged_in.get("/collect").text
    assert "nothing would be deleted" in body


def test_running_a_cleanup_deletes_what_the_preview_named(logged_in: TestClient) -> None:
    app = logged_in.app  # type: ignore[attr-defined]
    db = app.state.db

    job = enqueue(db, COLLECT_KIND, {"host_id": "old", "host_name": "old-host"})
    store_json(db, app.state.settings.data_dir, job_id=job.id, name="old-logs.tgz", payload={})
    mark_succeeded(db, job.id)
    db.execute("UPDATE jobs SET finished_at = 0, created_at = 0 WHERE id = ?", (job.id,))
    db.commit()

    response = logged_in.post("/collect/cleanup", data={"keep_days": 30, "keep_count": 0})
    assert "Deleted+1+collection" in response.headers["location"]
    assert list_jobs(db, kind=COLLECT_KIND) == []


def test_a_cleanup_deleting_several_says_collections_not_collection(
    logged_in: TestClient,
) -> None:
    app = logged_in.app  # type: ignore[attr-defined]
    db = app.state.db

    for name in ("old-one", "old-two"):
        job = enqueue(db, COLLECT_KIND, {"host_id": name, "host_name": name})
        store_json(db, app.state.settings.data_dir, job_id=job.id, name="logs.tgz", payload={})
        mark_succeeded(db, job.id)
        db.execute("UPDATE jobs SET finished_at = 0, created_at = 0 WHERE id = ?", (job.id,))
    db.commit()

    response = logged_in.post("/collect/cleanup", data={"keep_days": 30, "keep_count": 0})
    location = response.headers["location"]
    assert "Deleted+2+collections," in location
    assert "collection(s)" not in location


def test_a_cleanup_with_nothing_to_do_says_so_rather_than_claiming_a_deletion(
    logged_in: TestClient,
) -> None:
    response = logged_in.post("/collect/cleanup", data={"keep_days": 30, "keep_count": 3})
    assert "Nothing+was+old+enough" in response.headers["location"]


def test_a_restricted_connection_is_warned_about_before_collecting(
    logged_in: TestClient,
) -> None:
    """A restricted account will be refused, and should hear it here first."""
    app = logged_in.app  # type: ignore[attr-defined]
    save_connection(
        app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="restricted",
        verify_tls=True,
        secret_key=app.state.settings.secret_key,
    )

    body = logged_in.get("/collect").text
    assert "restricted account" in body
    assert "export:logs" in body


def test_an_admin_connection_is_not_warned_about(logged_in: TestClient) -> None:
    """The other side of the condition.

    Shipped stuck on: the template read an attribute the connection did not
    have, so every account saw the restricted-account warning. Asserting only
    that a restricted account is warned cannot catch that.
    """
    app = logged_in.app  # type: ignore[attr-defined]
    save_connection(
        app.state.db,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=app.state.settings.secret_key,
    )

    body = logged_in.get("/collect").text
    assert "restricted account" not in body


def test_the_inventory_the_page_uses_is_the_stored_one(with_inventory: TestClient) -> None:
    """The page must not need XO to be reachable to show what was collected.

    Nothing is patched here, so any call to Xen Orchestra would fail the
    request rather than render the host.
    """
    assert HOST.name in with_inventory.get("/collect").text
    assert INVENTORY_KIND  # the stored result this reads comes from that job


def test_deleting_a_failed_collection_works_from_the_page(logged_in: TestClient) -> None:
    """The path actually pressed when a collection fails.

    Reported from a real run: the button appeared to do nothing, because the
    lookup behind it listed only successful collections.
    """
    app = logged_in.app  # type: ignore[attr-defined]
    db = app.state.db

    job = enqueue(db, COLLECT_KIND, {"host_id": "h", "host_name": "Xcp-Ng-Host1"})
    store_json(db, app.state.settings.data_dir, job_id=job.id, name="partial.tgz", payload={})
    db.execute("UPDATE jobs SET state = 'failed', finished_at = 1 WHERE id = ?", (job.id,))
    db.commit()

    response = logged_in.post(f"/collect/{job.id}/delete")
    assert response.status_code == 303
    assert "notice" in response.headers["location"]
    assert list_jobs(db, kind=COLLECT_KIND) == []


def test_a_failed_collection_offers_its_delete_button(logged_in: TestClient) -> None:
    app = logged_in.app  # type: ignore[attr-defined]
    db = app.state.db

    job = enqueue(db, COLLECT_KIND, {"host_id": "h", "host_name": "Xcp-Ng-Host1"})
    db.execute(
        "UPDATE jobs SET state = 'failed', finished_at = 1, error = 'boom' WHERE id = ?",
        (job.id,),
    )
    db.commit()

    body = logged_in.get("/collect").text
    assert f"/collect/{job.id}/delete" in body
    assert "Delete this collection" in body


# ---- extracting log categories -------------------------------------------


def test_ticking_categories_at_collection_time_queues_an_extraction_after_it(
    with_inventory: TestClient,
) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]

    with_inventory.post("/collect", data={"host_id": HOST.id, "categories": ["xapi"]})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)

    collect_job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    extract_job = list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0]
    assert extract_job.params["source_job_id"] == collect_job.id
    assert extract_job.params["categories"] == ["xapi"]
    assert extract_job.state == "succeeded"


def test_no_categories_ticked_queues_no_extraction(with_inventory: TestClient) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]

    with_inventory.post("/collect", data={"host_id": HOST.id})
    assert list_jobs(app.state.db, kind=EXTRACT_KIND) == []


def test_extracting_from_an_existing_collection(with_inventory: TestClient) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    response = with_inventory.post(f"/collect/{job_id}/extract", data={"categories": ["xapi"]})
    assert response.status_code == 303
    assert "error" not in response.headers["location"]

    extract_job = list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0]
    assert extract_job.params["artifact_id"]
    assert extract_job.params["categories"] == ["xapi"]

    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)
    assert list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0].state == "succeeded"


def test_extracting_with_no_category_ticked_is_refused(with_inventory: TestClient) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    response = with_inventory.post(f"/collect/{job_id}/extract", data={})
    assert "error" in response.headers["location"]
    assert list_jobs(app.state.db, kind=EXTRACT_KIND) == []


def test_extracting_from_an_unknown_collection_is_refused(with_inventory: TestClient) -> None:
    response = with_inventory.post("/collect/not-a-job/extract", data={"categories": ["xapi"]})
    assert "error" in response.headers["location"]


def test_the_page_offers_extraction_for_a_finished_collection(with_inventory: TestClient) -> None:
    _collect(with_inventory)
    body = with_inventory.get("/collect").text
    assert "Extract specific log categories from this bundle" in body


def test_a_completed_extraction_shows_its_download_on_the_page(
    with_inventory: TestClient,
) -> None:
    """The extraction's own archive has a download link, not just its report.

    Reported from a real screenshot: the finished extraction card rendered its
    rule table and a delete button but no download link at all, because the
    page built its artifacts-by-job-id map from collection jobs only —
    ``artifacts.get(extraction.id, [])`` was always empty for an extraction.
    """
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    with_inventory.post(f"/collect/{job_id}/extract", data={"categories": ["xapi"]})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)

    body = with_inventory.get("/collect").text
    extract_job = list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0]
    archive = next(
        item for item in list_for_job(app.state.db, extract_job.id) if item.name.endswith(".tgz")
    )
    assert f"/collect/download/{archive.id}" in body
    assert f"/collect/extractions/{extract_job.id}/delete" in body


def test_deleting_an_extraction_does_not_touch_its_collection(
    with_inventory: TestClient,
) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    with_inventory.post(f"/collect/{job_id}/extract", data={"categories": ["xapi"]})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)
    extract_job = list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0]

    response = with_inventory.post(f"/collect/extractions/{extract_job.id}/delete")
    assert "notice" in response.headers["location"]
    assert list_jobs(app.state.db, kind=EXTRACT_KIND) == []
    assert list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0].id == job_id


def test_an_extraction_job_id_cannot_delete_its_collection(with_inventory: TestClient) -> None:
    """The delete route for extractions is restricted to EXTRACT_KIND, so an
    extraction job's id posted to the collection's own delete route does
    nothing — the two kinds must never be able to delete each other's rows.
    """
    app = with_inventory.app  # type: ignore[attr-defined]
    job_id = _collect(with_inventory)

    with_inventory.post(f"/collect/{job_id}/extract", data={"categories": ["xapi"]})
    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        run_pending_jobs(app)
    extract_job = list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0]

    response = with_inventory.post(f"/collect/{extract_job.id}/delete")
    assert "error" in response.headers["location"]
    assert list_jobs(app.state.db, kind=EXTRACT_KIND, limit=1)[0].id == extract_job.id
