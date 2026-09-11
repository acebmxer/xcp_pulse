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

from app.artifacts import artifact_path, store_file, store_json
from app.db import init_db
from app.job_collect import KIND as COLLECT_KIND
from app.job_findings import FINDINGS_ARTIFACT, FINDINGS_MARKDOWN
from app.job_findings import KIND as FINDINGS_KIND
from app.job_inventory import INVENTORY_ARTIFACT
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_redact import REPORT_ARTIFACT as REDACTION_REPORT_ARTIFACT
from app.job_runner import JobWorker
from app.job_support_package import KIND, MANIFEST_NAME, package_from_job
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job, mark_failed, mark_succeeded

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
