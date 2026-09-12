"""The Support package job: builds one archive from three sibling jobs.

Mirrors `test_job_extract.py`'s pattern — the sibling jobs (collection,
findings, inventory) are enqueued and marked succeeded directly with their
artifacts pre-stored, rather than actually run, so this suite exercises only
`job_support_package` and not the jobs it depends on.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.artifacts import artifact_path, list_for_job, store_file, store_json
from app.db import init_db
from app.job_collect import KIND as COLLECT_KIND
from app.job_findings import FINDINGS_ARTIFACT, FINDINGS_MARKDOWN
from app.job_findings import KIND as FINDINGS_KIND
from app.job_inventory import INVENTORY_ARTIFACT
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_redact import KIND as REDACT_KIND
from app.job_redact import REPORT_ARTIFACT as REDACTION_REPORT_ARTIFACT
from app.job_runner import JobWorker
from app.job_support_package import KIND, MANIFEST_NAME, package_from_job
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job, mark_failed, mark_succeeded
from app.redact import RULES

HOST_NAME = "xcp-ng-host1"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


class _Settings:
    secret_key = "test-secret-key-not-for-production"


@pytest.fixture
def worker(tmp_path: Path) -> JobWorker:
    return JobWorker(tmp_path / "test.db", tmp_path, _Settings())


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


def _store_collection(conn, data_dir, host_name: str = HOST_NAME) -> str:
    """A finished collection with a redacted bundle and a redaction report."""
    job = enqueue(conn, COLLECT_KIND, {"host_id": "host-1", "host_name": host_name})
    mark_succeeded(conn, job.id)

    working = data_dir / "artifacts" / job.id / "raw.tmp"
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_bytes(b"redacted bundle bytes")
    store_file(
        conn,
        data_dir,
        job_id=job.id,
        name=f"{host_name}-logs.redacted.tgz",
        media_type="application/gzip",
        source=working,
    )
    store_json(
        conn,
        data_dir,
        job_id=job.id,
        name=REDACTION_REPORT_ARTIFACT,
        payload={"rules_disabled": ["ipv4"], "total_hits": 3},
    )
    return job.id


def _store_raw_only_collection(conn, data_dir, host_name: str = HOST_NAME) -> str:
    """A finished collection that was told to skip redaction — raw bundle only.

    The case this exists for: the Collect page's redaction checkbox left
    unticked, so the collection has no redacted copy and no report at all for
    a package to read directly.
    """
    params = {"host_id": "host-1", "host_name": host_name, "redact": False}
    job = enqueue(conn, COLLECT_KIND, params)
    mark_succeeded(conn, job.id)

    working = data_dir / "artifacts" / job.id / "raw.tmp"
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_bytes(b"raw bundle bytes")
    store_file(
        conn,
        data_dir,
        job_id=job.id,
        name=f"{host_name}-logs.tgz",
        media_type="application/gzip",
        source=working,
    )
    return job.id


def _store_findings(conn, data_dir) -> str:
    job = enqueue(conn, FINDINGS_KIND, {})
    mark_succeeded(conn, job.id)
    store_json(conn, data_dir, job_id=job.id, name=FINDINGS_ARTIFACT, payload={"findings": []})

    working = data_dir / "artifacts" / job.id / "md.tmp"
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_text("# Findings\n", encoding="utf-8")
    store_file(
        conn,
        data_dir,
        job_id=job.id,
        name=FINDINGS_MARKDOWN,
        media_type="text/markdown",
        source=working,
    )
    return job.id


def _store_inventory(conn, data_dir) -> str:
    job = enqueue(conn, INVENTORY_KIND, {})
    mark_succeeded(conn, job.id)
    store_json(
        conn, data_dir, job_id=job.id, name=INVENTORY_ARTIFACT, payload={"pools": [], "hosts": []}
    )
    return job.id


def _enqueue_package(conn, *, collect_job_id, findings_job_id, inventory_job_id):
    return enqueue(
        conn,
        KIND,
        {
            "source_job_id": collect_job_id,
            "findings_job_id": findings_job_id,
            "inventory_job_id": inventory_job_id,
        },
    )


def test_package_bundles_every_file_with_a_manifest(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    collect_job_id = _store_collection(conn, data_dir)
    findings_job_id = _store_findings(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)

    job = _enqueue_package(
        conn,
        collect_job_id=collect_job_id,
        findings_job_id=findings_job_id,
        inventory_job_id=inventory_job_id,
    )
    worker.run_one(conn)

    assert get_job(conn, job.id).state == SUCCEEDED
    package = package_from_job(conn, job.id)
    assert package is not None
    assert package.name == f"{HOST_NAME}-support-package.tgz"

    archive_path = artifact_path(data_dir, job.id, package.id)
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())
        assert f"{HOST_NAME}-logs.redacted.tgz" in names
        assert FINDINGS_ARTIFACT in names
        assert FINDINGS_MARKDOWN in names
        assert INVENTORY_ARTIFACT in names
        assert REDACTION_REPORT_ARTIFACT in names
        assert MANIFEST_NAME in names

        manifest_member = archive.extractfile(MANIFEST_NAME)
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read())

        # Not just present by name — the bytes inside must be the real
        # content, not a truncated or empty placeholder. This is the same
        # class of bug as the incomplete-tar-member issue found in a real
        # collection: a member that opens but reads back wrong or empty.
        bundle_member = archive.extractfile(f"{HOST_NAME}-logs.redacted.tgz")
        assert bundle_member is not None
        assert bundle_member.read() == b"redacted bundle bytes"

        findings_member = archive.extractfile(FINDINGS_ARTIFACT)
        assert findings_member is not None
        assert json.loads(findings_member.read()) == {"findings": []}

    assert manifest["host_name"] == HOST_NAME
    assert manifest["rules_disabled"] == ["ipv4"]
    assert manifest["findings_job_id"] == findings_job_id
    assert manifest["inventory_job_id"] == inventory_job_id
    assert set(manifest["files"]) == {
        f"{HOST_NAME}-logs.redacted.tgz",
        FINDINGS_ARTIFACT,
        FINDINGS_MARKDOWN,
        INVENTORY_ARTIFACT,
        REDACTION_REPORT_ARTIFACT,
    }


def test_a_raw_only_collection_is_packaged_via_a_chained_redaction(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """The collect-raw-then-redact-later path, packaged end to end.

    A collection stored with redaction switched off has no redacted bundle and
    no report of its own — the package chain queues a real ``redact_artifact``
    job against it (``source_job_id``, the same deferred resolution
    ``job_extract`` uses) and the package reads that job's output instead of
    demanding the collection have produced it directly.
    """
    collect_job_id = _store_raw_only_collection(conn, data_dir)
    findings_job_id = _store_findings(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)
    redact_job = enqueue(conn, REDACT_KIND, {"source_job_id": collect_job_id})

    job = enqueue(
        conn,
        KIND,
        {
            "source_job_id": collect_job_id,
            "findings_job_id": findings_job_id,
            "inventory_job_id": inventory_job_id,
            "redact_job_id": redact_job.id,
        },
    )
    # FIFO: the redaction enqueued just above runs before the package that
    # depends on it, the same ordering the route guarantees in production.
    worker.run_one(conn)
    worker.run_one(conn)

    assert get_job(conn, redact_job.id).state == SUCCEEDED
    assert get_job(conn, job.id).state == SUCCEEDED
    package = package_from_job(conn, job.id)
    assert package is not None

    archive_path = artifact_path(data_dir, job.id, package.id)
    with tarfile.open(archive_path, "r:gz") as archive:
        assert f"{HOST_NAME}-logs.redacted.tgz" in archive.getnames()
        assert REDACTION_REPORT_ARTIFACT in archive.getnames()


def test_a_second_package_reuses_an_earlier_redaction_instead_of_repeating_it(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """Building a second package from the same raw-only collection must not
    re-redact a bundle a prior job already redacted.

    Reported as a real bug: the route checked the collection's own id for a
    redaction report, but a chained ``redact_artifact`` job's report lives
    under *that job's* id — so the check always read "not redacted" for a
    raw-only collection, even after it plainly had been, and every package
    built from it queued a fresh 433 MB redaction. This queues no
    ``redact_job_id`` at all, the shape a second package build takes once the
    route's own check finds the earlier redaction, and proves the job itself
    finds that earlier work rather than failing or repeating it.
    """
    collect_job_id = _store_raw_only_collection(conn, data_dir)
    earlier_redact_job = enqueue(conn, REDACT_KIND, {"source_job_id": collect_job_id})
    mark_succeeded(conn, earlier_redact_job.id)
    store_file(
        conn,
        data_dir,
        job_id=earlier_redact_job.id,
        name=f"{HOST_NAME}-logs.redacted.tgz",
        media_type="application/gzip",
        source=_write(data_dir, earlier_redact_job.id, "redacted.tmp", b"already redacted"),
    )
    store_json(
        conn,
        data_dir,
        job_id=earlier_redact_job.id,
        name=REDACTION_REPORT_ARTIFACT,
        payload={
            "source": {"artifact_id": _raw_bundle_id(conn, collect_job_id)},
            # Every rule marked enabled, matching the default `enabled_rules`
            # returns on a fresh database (all rules on) — `existing_redaction`
            # matches by this same rules-enabled set, not just by source id.
            "rules": [{"name": rule.name, "enabled": True} for rule in RULES],
            "rules_disabled": [],
            "total_hits": 0,
        },
    )
    findings_job_id = _store_findings(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)

    # No redact_job_id: this is the shape the route sends once its own check
    # finds the redaction already stored above.
    job = _enqueue_package(
        conn,
        collect_job_id=collect_job_id,
        findings_job_id=findings_job_id,
        inventory_job_id=inventory_job_id,
    )
    worker.run_one(conn)

    result = get_job(conn, job.id)
    assert result.state == SUCCEEDED
    # Only the package job ran — no second redact_artifact job was queued or
    # claimed, so the earlier redaction's own artifacts are exactly what was
    # packaged, untouched.
    assert get_job(conn, earlier_redact_job.id).state == SUCCEEDED

    package = package_from_job(conn, job.id)
    archive_path = artifact_path(data_dir, job.id, package.id)
    with tarfile.open(archive_path, "r:gz") as archive:
        bundle_member = archive.extractfile(f"{HOST_NAME}-logs.redacted.tgz")
        assert bundle_member is not None
        assert bundle_member.read() == b"already redacted"


def _write(data_dir: Path, job_id: str, name: str, body: bytes) -> Path:
    path = data_dir / "artifacts" / job_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _raw_bundle_id(conn: sqlite3.Connection, collect_job_id: str) -> str:
    return next(
        item.id for item in list_for_job(conn, collect_job_id) if item.name.endswith("-logs.tgz")
    )


def test_package_fails_when_a_raw_only_collection_has_no_redaction_queued(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """Without a chained redaction, a raw-only collection is a clear failure,
    not a package silently missing its main file."""
    collect_job_id = _store_raw_only_collection(conn, data_dir)
    findings_job_id = _store_findings(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)

    job = _enqueue_package(
        conn,
        collect_job_id=collect_job_id,
        findings_job_id=findings_job_id,
        inventory_job_id=inventory_job_id,
    )
    worker.run_one(conn)

    result = get_job(conn, job.id)
    assert result.state == FAILED
    assert "redaction report" in result.error.lower()


def test_package_fails_when_the_collection_failed(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    collect_job = enqueue(conn, COLLECT_KIND, {"host_id": "host-1", "host_name": HOST_NAME})
    mark_failed(conn, collect_job.id, "no connection")
    findings_job_id = _store_findings(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)

    job = _enqueue_package(
        conn,
        collect_job_id=collect_job.id,
        findings_job_id=findings_job_id,
        inventory_job_id=inventory_job_id,
    )
    worker.run_one(conn)

    result = get_job(conn, job.id)
    assert result.state == FAILED
    assert "collection" in result.error.lower()


def test_package_fails_when_findings_is_missing(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    collect_job_id = _store_collection(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)

    job = enqueue(
        conn,
        KIND,
        {
            "source_job_id": collect_job_id,
            "findings_job_id": "does-not-exist",
            "inventory_job_id": inventory_job_id,
        },
    )
    worker.run_one(conn)

    result = get_job(conn, job.id)
    assert result.state == FAILED
    assert "findings" in result.error.lower()


def test_package_from_job_finds_nothing_for_an_unrelated_job(
    conn: sqlite3.Connection, data_dir: Path
) -> None:
    other_job = enqueue(conn, COLLECT_KIND, {"host_id": "host-1", "host_name": HOST_NAME})
    assert package_from_job(conn, other_job.id) is None


def _store_raw_only_collection_with_real_bundle(conn, data_dir, host_name: str = HOST_NAME) -> str:
    """Like ``_store_raw_only_collection``, but with an actual tar.gz body —
    needed for a test that runs a real ``extract_categories`` job against it,
    rather than only ``job_support_package`` reading the stored bytes back
    verbatim."""
    import io

    params = {"host_id": "host-1", "host_name": host_name, "redact": False}
    job = enqueue(conn, COLLECT_KIND, params)
    mark_succeeded(conn, job.id)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        body = b"Sep  6 12:30:45 xen01 xapi: started\n"
        info = tarfile.TarInfo("var/log/xensource.log")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))

    working = data_dir / "artifacts" / job.id / "raw.tmp"
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_bytes(buffer.getvalue())
    store_file(
        conn,
        data_dir,
        job_id=job.id,
        name=f"{host_name}-logs.tgz",
        media_type="application/gzip",
        source=working,
    )
    return job.id


def test_a_date_ranged_package_ships_the_extraction_instead_of_the_full_bundle(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """A date range chains a real ``extract_categories`` job ahead of the
    package, and the package ships *that* archive and report — not the
    collection's own full redacted copy — per ``job_support_package.run``.
    """
    from app.job_extract import KIND as EXTRACT_KIND
    from app.log_categories import category_keys

    collect_job_id = _store_raw_only_collection_with_real_bundle(conn, data_dir)
    findings_job_id = _store_findings(conn, data_dir)
    inventory_job_id = _store_inventory(conn, data_dir)

    extract_job = enqueue(
        conn,
        EXTRACT_KIND,
        {
            "artifact_id": _raw_bundle_id(conn, collect_job_id),
            "categories": list(category_keys()),
            "include_rotated": True,
            "date_preset": "30d",
        },
    )

    job = enqueue(
        conn,
        KIND,
        {
            "source_job_id": collect_job_id,
            "findings_job_id": findings_job_id,
            "inventory_job_id": inventory_job_id,
            "extract_job_id": extract_job.id,
        },
    )
    # FIFO: the extraction enqueued first runs before the package that depends
    # on it, the same ordering the route guarantees in production.
    worker.run_one(conn)
    worker.run_one(conn)

    assert get_job(conn, extract_job.id).state == SUCCEEDED
    assert get_job(conn, job.id).state == SUCCEEDED

    package = package_from_job(conn, job.id)
    assert package is not None
    archive_path = artifact_path(data_dir, job.id, package.id)
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
        # The extraction's own output name, not the collection's
        # "-logs.redacted.tgz" — proves the extraction's bundle was packaged,
        # not the (nonexistent, for a raw-only collection) full redacted one.
        assert any(name.endswith(".tgz") and "logs.redacted" not in name for name in names)

        manifest_member = archive.extractfile(MANIFEST_NAME)
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read())
    assert manifest["date_start"] is not None
    assert manifest["date_end"] is None
