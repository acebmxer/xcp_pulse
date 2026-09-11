"""The Support Package page: packaging an existing collection, and Collect + Package.

Runs the whole chain — collect, findings, inventory, package — through the real
route and the real FIFO queue via ``run_pending_jobs``, mocking only what talks
to Xen Orchestra. What matters here is that the chain a route enqueues actually
produces a downloadable package, not just that ``job_support_package`` can
assemble one given artifacts placed there by hand (that is
``test_job_support_package.py``'s job).
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.findings import Report
from app.job_collect import KIND as COLLECT_KIND
from app.job_support_package import KIND as SUPPORT_PACKAGE_KIND
from app.jobs import list_jobs
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
    app = connected.app  # type: ignore[attr-defined]
    connected.post("/jobs/refresh-inventory")
    with patch("app.job_inventory.build_client") as build:
        build.return_value.inventory.return_value = Inventory(pools=[POOL], hosts=[HOST])
        run_pending_jobs(app)
    yield connected


def _bundle() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        body = b"Sep  6 12:30:46 xen01 xapi: host at 10.20.30.41\n"
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
        destination.write_bytes(b"audit\n")
        return 6

    def inventory(self):
        return Inventory(pools=[POOL], hosts=[HOST])


def _run_chain(app) -> None:
    """Run every queued job with XO calls faked, the same fakes for all three."""
    with (
        patch("app.job_collect.build_client", return_value=_FakeClient()),
        patch("app.job_findings.build_client", return_value=_FakeClient()),
        patch("app.job_findings.collect_findings", return_value=Report()),
        patch("app.job_inventory.build_client", return_value=_FakeClient()),
    ):
        run_pending_jobs(app, limit=20)


def test_the_page_requires_login(client: TestClient) -> None:
    assert client.get("/support-package").status_code == 303


def test_collect_and_package_builds_a_downloadable_archive(with_inventory: TestClient) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]

    response = with_inventory.post("/support-package/collect", data={"host_id": HOST.id})
    assert response.status_code == 303

    _run_chain(app)

    package_jobs = list_jobs(app.state.db, kind=SUPPORT_PACKAGE_KIND, limit=1)
    assert package_jobs
    assert package_jobs[0].state == "succeeded"

    body = with_inventory.get("/support-package").text
    assert "support-package.tgz" in body
    assert "Download" in body


def test_collect_and_package_shows_progress_before_any_job_has_run(
    with_inventory: TestClient,
) -> None:
    """The page must show something moving the instant the chain is queued.

    Reported from a real screenshot: after pressing Collect + Package, the
    page showed no progress at all for the running collection, and only the
    Jobs page showed anything happening. The cause was two-fold — active
    collections were filtered out of ``collections`` entirely (only
    ``SUCCEEDED`` ones were listed), and ``any_active`` only looked at
    ``support_package``-kind jobs, so the page's own auto-refresh meta tag
    never fired.
    """
    app = with_inventory.app  # type: ignore[attr-defined]

    with_inventory.post("/support-package/collect", data={"host_id": HOST.id})

    body = with_inventory.get("/support-package").text
    assert HOST.name in body
    # The collection card itself must show progress, not just exist with a
    # bare state badge and a disabled button.
    assert "progress-bar" in body
    assert "queued" in body.lower() or "running" in body.lower()
    # The page must keep auto-refreshing while the chain is still going.
    assert 'http-equiv="refresh"' in body

    _run_chain(app)
    package_jobs = list_jobs(app.state.db, kind=SUPPORT_PACKAGE_KIND, limit=1)
    assert package_jobs[0].state == "succeeded"

    # The collection card itself must show a duration, not only its nested
    # Support Package card — missing entirely on a first pass at this
    # template, reported from a screenshot where the collection ran with a
    # progress bar but no elapsed counter. Checked on the slice of the page
    # up to the nested "Support package" card, since that card has its own
    # job-duration span too and a bare substring check cannot tell them apart.
    finished_body = with_inventory.get("/support-package").text
    collection_card = finished_body.split("Support package</span>")[0]
    assert "job-duration" in collection_card


def test_packaging_an_existing_collection_reuses_its_bundle(with_inventory: TestClient) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]

    with patch("app.job_collect.build_client", return_value=_FakeClient()):
        with_inventory.post("/collect", data={"host_id": HOST.id})
        run_pending_jobs(app)

    collect_job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    assert collect_job.state == "succeeded"

    response = with_inventory.post(f"/support-package/{collect_job.id}/package")
    assert response.status_code == 303

    _run_chain(app)

    package_jobs = list_jobs(app.state.db, kind=SUPPORT_PACKAGE_KIND, limit=1)
    assert package_jobs[0].state == "succeeded"
    assert package_jobs[0].params["source_job_id"] == collect_job.id


def test_a_package_stays_listed_after_its_source_collection_is_deleted(
    with_inventory: TestClient,
) -> None:
    """A built package must not vanish from the page when its collection goes.

    The archive is self-contained — it does not need the raw collection to
    exist any more — so deleting the collection on the Collect page (which
    does not know or care about support packages) must not make an already
    finished package disappear from this page too. It is still on disk, still
    in the database, and still downloadable by direct link; the page must
    still say so.
    """
    app = with_inventory.app  # type: ignore[attr-defined]
    with_inventory.post("/support-package/collect", data={"host_id": HOST.id})
    _run_chain(app)

    collect_job = list_jobs(app.state.db, kind=COLLECT_KIND, limit=1)[0]
    package_job = list_jobs(app.state.db, kind=SUPPORT_PACKAGE_KIND, limit=1)[0]
    assert package_job.params["source_job_id"] == collect_job.id

    with_inventory.post(f"/collect/{collect_job.id}/delete")

    body = with_inventory.get("/support-package").text
    assert "support-package.tgz" in body
    assert "Download" in body


def test_packaging_an_unknown_collection_is_refused(with_inventory: TestClient) -> None:
    response = with_inventory.post("/support-package/not-a-job/package")
    assert response.status_code == 303
    assert "no+longer+stored" in response.headers["location"]


def test_delete_removes_the_package_and_its_file(with_inventory: TestClient) -> None:
    app = with_inventory.app  # type: ignore[attr-defined]
    with_inventory.post("/support-package/collect", data={"host_id": HOST.id})
    _run_chain(app)

    package_job = list_jobs(app.state.db, kind=SUPPORT_PACKAGE_KIND, limit=1)[0]

    response = with_inventory.post(f"/support-package/{package_job.id}/delete")
    assert response.status_code == 303
    assert "deleted" in response.headers["location"].lower()

    body = with_inventory.get("/support-package").text
    assert "support-package.tgz" not in body
