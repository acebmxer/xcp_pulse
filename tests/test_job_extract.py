"""The Extract categories job: pulling selected log families from a bundle.

Mirrors ``test_job_collect.py``'s pattern — a real tarball built in memory,
run through the worker for real, then the produced archive opened and its
actual members checked rather than trusting the report's counts.
"""

from __future__ import annotations

import io
import sqlite3
import tarfile
from datetime import UTC
from pathlib import Path

import pytest

from app.artifacts import list_for_job, store_file
from app.db import init_db
from app.job_collect import KIND as COLLECT_KIND
from app.job_extract import KIND, report_from_job
from app.job_runner import JobWorker
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job, mark_failed, mark_succeeded

HOST_ID = "host-1"
HOST_NAME = "xcp-ng-host1"


def _bundle_bytes(
    members: dict[str, str],
    *,
    directories: tuple[str, ...] = (),
    mtimes: dict[str, float] | None = None,
) -> bytes:
    """Build a tar.gz. ``directories`` adds bare directory entries (no body),
    the way ``xen-bugtool`` bundles carry them for every real path — needed to
    test that a filtered extraction preserves them, not just files.
    ``mtimes``, keyed by member name, sets a member's modification time for
    tests of date-range filtering; a member not named there gets tarfile's
    own default (the current time)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in directories:
            info = tarfile.TarInfo(name.rstrip("/") + "/")
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, text in members.items():
            body = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(body)
            if mtimes and name in mtimes:
                info.mtime = mtimes[name]
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


# A small stand-in bundle covering several categories, current and rotated.
_MEMBERS = {
    "var/log/xensource.log": "Sep  6 12:30:45 xen01 xapi: host at 10.20.30.41\n",
    "var/log/xensource.log.1.gz": "old xapi line\n",
    "var/log/SMlog": "Sep  6 12:31:02 xen01 SM: mount 10.20.30.9\n",
    "var/log/daemon.log": "Sep  6 12:31:10 xen01 daemon: started\n",
    "var/log/audit.log": "Sep  6 12:32:00 xapi audit: session from 10.20.30.77\n",
}


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


def _store_bundle(
    conn,
    data_dir,
    members: dict[str, str] | None = None,
    *,
    directories: tuple[str, ...] = (),
    mtimes: dict[str, float] | None = None,
) -> tuple[str, str]:
    """Store a raw collected bundle as ``job_collect`` would, returning its artifact id.

    The collection job is enqueued and immediately marked succeeded rather
    than actually run: this test suite is not exercising ``job_collect``, and
    a real collection queued here would sit ahead of the extraction job in the
    FIFO queue and be the one ``worker.run_one`` claims first — with no
    connection configured, that fails the collection instead of ever reaching
    the extraction under test.
    """
    collect_job = enqueue(conn, COLLECT_KIND, {"host_id": HOST_ID, "host_name": HOST_NAME})
    mark_succeeded(conn, collect_job.id)
    body = _bundle_bytes(members or _MEMBERS, directories=directories, mtimes=mtimes)
    working = data_dir / "artifacts" / collect_job.id / "raw.tmp"
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_bytes(body)
    artifact = store_file(
        conn,
        data_dir,
        job_id=collect_job.id,
        name=f"{HOST_NAME}-logs.tgz",
        media_type="application/gzip",
        source=working,
    )
    return artifact.id, collect_job.id


def _members(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            body = archive.extractfile(member)
            out[member.name] = body.read().decode("utf-8") if body else ""
    return out


def _all_member_names(path: Path) -> set[str]:
    """Every member's name, files and directories alike — unlike ``_members``,
    which only reads file bodies and so cannot see whether a directory entry
    made it into the output."""
    with tarfile.open(path, "r:*") as archive:
        return {member.name for member in archive}


def test_extraction_keeps_only_the_selected_categories(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(conn, KIND, {"artifact_id": artifact_id, "categories": ["xapi"]})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == SUCCEEDED
    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)

    # Only the current xensource.log — audit and SMlog are different
    # categories, and the rotated xensource.log.1.gz is excluded by the
    # default "current logs only" behaviour.
    assert set(members) == {"var/log/xensource.log"}


def test_extraction_keeps_directory_entries_for_selected_categories(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """A directory belonging to a selected category survives extraction.

    Regression test: ``member_filter`` used to reject every non-file member
    outright, before it ever checked which category a directory belonged to —
    so an extracted archive never carried directory entries at all, contrary
    to ``_redact_tarball``'s own documented contract that a directory entry is
    offered to the filter and kept when it passes (job_collect.py).
    """
    members = {**_MEMBERS, "var/log/blktap/tapback.log": "Sep  6 12:33:00 tapback: ok\n"}
    artifact_id, _ = _store_bundle(conn, data_dir, members, directories=("var/log/blktap",))

    job = enqueue(conn, KIND, {"artifact_id": artifact_id, "categories": ["storage"]})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == SUCCEEDED
    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    names = _all_member_names(data_dir / "artifacts" / job.id / bundle.id)

    # tarfile itself strips a directory member's trailing "/" on read.
    assert "var/log/blktap" in names
    assert "var/log/blktap/tapback.log" in names
    # SMlog is a different file in the same "storage" category and must still
    # be present; xensource.log's directory-less top-level path is a different
    # category and must not leak in.
    assert "var/log/SMlog" in names


def test_include_rotated_pulls_in_rotated_history_too(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(
        conn,
        KIND,
        {"artifact_id": artifact_id, "categories": ["xapi"], "include_rotated": True},
    )
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)
    assert set(members) == {"var/log/xensource.log", "var/log/xensource.log.1.gz"}


def test_multiple_categories_land_in_one_combined_archive(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(
        conn, KIND, {"artifact_id": artifact_id, "categories": ["xapi", "storage", "audit"]}
    )
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    # Exactly one archive, not one per category — matches the decided design.
    archives = [item for item in produced if item.name.endswith(".tgz")]
    assert len(archives) == 1
    members = _members(data_dir / "artifacts" / job.id / archives[0].id)
    assert set(members) == {"var/log/xensource.log", "var/log/SMlog", "var/log/audit.log"}


def test_extracted_text_is_masked_the_same_as_a_full_redaction(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(
        conn, data_dir, {"var/log/SMlog": "mount 10.20.30.9 from host\n"}
    )

    job = enqueue(conn, KIND, {"artifact_id": artifact_id, "categories": ["storage"]})
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)
    assert "10.20.30.9" not in members["var/log/SMlog"]
    assert "[IPv4]" in members["var/log/SMlog"]


def test_no_category_selected_fails_the_job(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(conn, KIND, {"artifact_id": artifact_id, "categories": []})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == FAILED
    assert "No log category was selected" in get_job(conn, job.id).error


def test_a_category_with_nothing_in_the_bundle_succeeds_with_an_empty_archive(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(conn, KIND, {"artifact_id": artifact_id, "categories": ["ha"]})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == SUCCEEDED
    assert "No files matched" in get_job(conn, job.id).step


def test_extracting_from_a_redacted_copy_is_refused(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """Extraction reads the raw bundle, never a redacted copy — masking twice
    over would make a report of "what was masked" wrong."""
    collect_job = enqueue(conn, COLLECT_KIND, {"host_id": HOST_ID, "host_name": HOST_NAME})
    mark_succeeded(conn, collect_job.id)
    working = data_dir / "artifacts" / collect_job.id / "redacted.tmp"
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_bytes(_bundle_bytes(_MEMBERS))
    artifact = store_file(
        conn,
        data_dir,
        job_id=collect_job.id,
        name=f"{HOST_NAME}-logs.redacted.tgz",
        media_type="application/gzip",
        source=working,
    )

    job = enqueue(conn, KIND, {"artifact_id": artifact.id, "categories": ["xapi"]})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == FAILED
    assert "not a raw collected log bundle" in get_job(conn, job.id).error


def test_source_job_id_resolves_once_the_collection_has_finished(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """The "collect, then extract" path: queued before the bundle exists."""
    artifact_id, collect_job_id = _store_bundle(conn, data_dir)

    job = enqueue(conn, KIND, {"source_job_id": collect_job_id, "categories": ["xapi"]})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == SUCCEEDED
    report = report_from_job(conn, data_dir, job.id)
    assert report is not None
    assert report["categories"][0]["key"] == "xapi"


def test_a_failed_source_collection_fails_the_extraction_clearly(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    collect_job = enqueue(conn, COLLECT_KIND, {"host_id": HOST_ID, "host_name": HOST_NAME})
    mark_failed(conn, collect_job.id, "the download stalled")

    job = enqueue(conn, KIND, {"source_job_id": collect_job.id, "categories": ["xapi"]})
    worker.run_one(conn)

    assert get_job(conn, job.id).state == FAILED
    assert "failed" in get_job(conn, job.id).error


# -- date range -------------------------------------------------------------


def test_date_range_excludes_a_rotated_file_by_modification_time(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    import time
    from datetime import datetime

    now = time.time()
    old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    artifact_id, _ = _store_bundle(
        conn,
        data_dir,
        {
            "var/log/xensource.log": "current, unrotated\n",
            "var/log/xensource.log.1.gz": "old rotated history\n",
        },
        mtimes={"var/log/xensource.log": now, "var/log/xensource.log.1.gz": old},
    )

    job = enqueue(
        conn,
        KIND,
        {
            "artifact_id": artifact_id,
            "categories": ["xapi"],
            "include_rotated": True,
            "date_preset": "30d",
        },
    )
    worker.run_one(conn)

    assert get_job(conn, job.id).state == SUCCEEDED
    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)

    # The rotated file is outside the last-30-days window and is skipped
    # entirely; the current file has no rotation history to filter by mtime
    # and survives.
    assert set(members) == {"var/log/xensource.log"}


def test_date_range_keeps_a_rotated_file_inside_the_window(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    import time

    now = time.time()
    artifact_id, _ = _store_bundle(
        conn,
        data_dir,
        {
            "var/log/xensource.log": "current\n",
            "var/log/xensource.log.1.gz": "recent rotated history\n",
        },
        mtimes={"var/log/xensource.log": now, "var/log/xensource.log.1.gz": now - 3600},
    )

    job = enqueue(
        conn,
        KIND,
        {
            "artifact_id": artifact_id,
            "categories": ["xapi"],
            "include_rotated": True,
            "date_preset": "7d",
        },
    )
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)
    assert set(members) == {"var/log/xensource.log", "var/log/xensource.log.1.gz"}


def test_date_range_filters_lines_in_a_file_straddling_the_window(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """A current, unrotated file is never excluded by mtime, but a date range
    still narrows it line by line for content that straddles the edge."""
    content = (
        "2026-01-05T00:00:00 inside the window\n"
        "2026-03-01T00:00:00 outside the window\n"
        "no timestamp on this line, kept regardless\n"
    )
    artifact_id, _ = _store_bundle(conn, data_dir, {"var/log/xensource.log": content})

    job = enqueue(
        conn,
        KIND,
        {
            "artifact_id": artifact_id,
            "categories": ["xapi"],
            "date_preset": "custom",
            "date_start": "2026-01-01",
            "date_end": "2026-01-31",
        },
    )
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)
    body = members["var/log/xensource.log"]
    assert "inside the window" in body
    assert "outside the window" not in body
    assert "kept regardless" in body


def test_no_date_range_means_no_filtering(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    """Omitting the date fields entirely is unchanged behaviour: no line of a
    kept member is dropped, exactly as before this feature existed. (Content
    is still masked by the active redaction rules, which is unrelated.)"""
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(conn, KIND, {"artifact_id": artifact_id, "categories": ["xapi"]})
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    bundle = next(item for item in produced if item.name.endswith(".tgz"))
    members = _members(data_dir / "artifacts" / job.id / bundle.id)
    assert members["var/log/xensource.log"].count("\n") == _MEMBERS["var/log/xensource.log"].count(
        "\n"
    )


def test_date_range_recorded_in_the_report(
    conn: sqlite3.Connection, worker: JobWorker, data_dir: Path
) -> None:
    artifact_id, _ = _store_bundle(conn, data_dir)

    job = enqueue(
        conn,
        KIND,
        {
            "artifact_id": artifact_id,
            "categories": ["xapi"],
            "date_preset": "custom",
            "date_start": "2026-01-01",
            "date_end": "2026-01-31",
        },
    )
    worker.run_one(conn)

    report = report_from_job(conn, data_dir, job.id)
    assert report is not None
    assert report["date_start"] is not None
    assert report["date_end"] is not None
