"""The Redact artifact job: what it masks, what it stores, and what it reports.

The report is the reason this job exists, so most of what is checked here is
whether the counts describe what actually happened to the file. A report that
says eight IPv4 addresses were masked when the copy still contains one is worse
than no report at all — it is what someone reads before sending a bundle to
Vates.

The other half is the streaming: this must produce byte-for-byte what
``redact_text`` produces on the same input, or the preview page is lying about
what a real run does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.artifacts import artifact_path, get_artifact, list_for_job, store_file, store_json
from app.db import init_db
from app.job_redact import KIND, REPORT_ARTIFACT, redacted_name, report_from_job, report_rows
from app.job_runner import JobWorker
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job
from app.redact import RULES, redact_text, set_enabled_rules

SAMPLE = """\
Sep  6 12:30:45 xen01.pool1.internal xapi: trackid=a3f9c2b18e4d0c67 user=admin@pool1.internal
Sep  6 12:30:46 xen01.pool1.internal xapi: host 4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34 at 10.20.30.41
Sep  6 12:30:47 xen01.pool1.internal xenopsd: VIF 3a:4b:5c:6d:7e:8f on 10.20.30.55
Sep  6 12:30:48 xen01.pool1.internal SM: mount 10.20.30.9 password=Str0ngPass!
Sep  6 12:30:49 xen01.pool1.internal xapi: health check from 127.0.0.1 ok
"""


# The job that "produced" the file being redacted. Never queued, so the worker
# under test always claims the redaction job rather than this one.
SOURCE_JOB_ID = "0" * 32


class _Settings:
    secret_key = "test-secret-key-not-for-production"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


@pytest.fixture
def worker(tmp_path: Path) -> JobWorker:
    return JobWorker(tmp_path / "test.db", tmp_path, _Settings())


def _store_source(
    conn: sqlite3.Connection, tmp_path: Path, text: str, name: str = "xensource.log"
) -> str:
    """Put a file in the artifact store the way a collection job would.

    The owning job is inserted already finished rather than queued: a queued
    one would be claimed by the worker ahead of the redaction job under test,
    and no kind is registered for "the job that produced this".
    """
    _finished_source_job(conn)
    path = tmp_path / "incoming.log"
    path.write_text(text, encoding="utf-8")
    artifact = store_file(
        conn,
        tmp_path,
        job_id=SOURCE_JOB_ID,
        name=name,
        media_type="text/plain",
        source=path,
    )
    return artifact.id


def _finished_source_job(conn: sqlite3.Connection) -> None:
    """The job row the source artifact hangs off, in a terminal state."""
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs
            (id, kind, state, params, progress, step, created_at, finished_at)
        VALUES (?, 'source', ?, '{}', 100, '', 0, 0)
        """,
        (SOURCE_JOB_ID, SUCCEEDED),
    )
    conn.commit()


def _run(conn: sqlite3.Connection, worker: JobWorker, artifact_id: str) -> str:
    job = enqueue(conn, KIND, {"artifact_id": artifact_id})
    worker.run_one(conn)
    return job.id


def _read(tmp_path: Path, conn: sqlite3.Connection, job_id: str, name: str) -> str:
    artifact = next(item for item in list_for_job(conn, job_id) if item.name == name)
    return artifact_path(tmp_path, job_id, artifact.id).read_text(encoding="utf-8")


def test_it_stores_a_redacted_copy_and_a_report(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    job_id = _run(conn, worker, _store_source(conn, tmp_path, SAMPLE))

    assert get_job(conn, job_id).state == SUCCEEDED
    assert sorted(item.name for item in list_for_job(conn, job_id)) == [
        REPORT_ARTIFACT,
        "xensource.redacted.log",
    ]


def test_the_stored_copy_matches_what_the_preview_would_produce(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The streaming loop and ``redact_text`` must not diverge.

    They apply the same rules in the same order, so a difference here means one
    of them has been changed without the other — and the preview page would then
    be showing something a real run does not do.
    """
    job_id = _run(conn, worker, _store_source(conn, tmp_path, SAMPLE))

    expected, _ = redact_text(SAMPLE)
    assert _read(tmp_path, conn, job_id, "xensource.redacted.log") == expected


def test_the_counts_match_what_the_copy_actually_contains(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The report's numbers, checked against the placeholders in the file.

    Counting the placeholders is a different measurement from the one the job
    made, which is the point: a count produced by the same code it is checking
    would prove nothing.
    """
    job_id = _run(conn, worker, _store_source(conn, tmp_path, SAMPLE))

    masked = _read(tmp_path, conn, job_id, "xensource.redacted.log")
    report = report_from_job(conn, tmp_path, job_id)

    by_name = {row["name"]: row["hits"] for row in report["rules"]}
    for rule in RULES:
        assert masked.count(rule.placeholder) == by_name[rule.name], rule.name

    assert report["total_hits"] == sum(by_name.values())
    assert report["lines"] == len(SAMPLE.splitlines())


def test_loopback_is_left_alone_and_not_counted(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The keep list has to survive the streaming path too."""
    job_id = _run(conn, worker, _store_source(conn, tmp_path, SAMPLE))

    assert "127.0.0.1" in _read(tmp_path, conn, job_id, "xensource.redacted.log")


def test_a_switched_off_rule_masks_nothing_and_says_so(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The stored settings apply to a real run, not only to the preview.

    A report where a disabled rule silently reads zero hits would be
    indistinguishable from one where it found nothing, which is exactly the
    thing someone about to send a bundle needs to be able to tell apart.
    """
    set_enabled_rules(conn, [rule.name for rule in RULES if rule.name != "ipv4"])
    job_id = _run(conn, worker, _store_source(conn, tmp_path, SAMPLE))

    masked = _read(tmp_path, conn, job_id, "xensource.redacted.log")
    assert "10.20.30.41" in masked

    report = report_from_job(conn, tmp_path, job_id)
    assert report["rules_disabled"] == ["ipv4"]
    ipv4 = next(row for row in report["rules"] if row["name"] == "ipv4")
    assert ipv4 == {
        "name": "ipv4",
        "title": ipv4["title"],
        "placeholder": "[IPv4]",
        "enabled": False,
        "hits": 0,
    }


def test_the_report_names_both_files_by_hash(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """What the report is for: saying which file this describes.

    Without the hashes the report is a set of numbers with nothing to attach
    them to once the bundle has been copied somewhere else.
    """
    source_id = _store_source(conn, tmp_path, SAMPLE)
    job_id = _run(conn, worker, source_id)

    report = report_from_job(conn, tmp_path, job_id)
    redacted = next(
        item for item in list_for_job(conn, job_id) if item.name == "xensource.redacted.log"
    )

    assert report["source"]["artifact_id"] == source_id
    assert report["source"]["name"] == "xensource.log"
    assert report["redacted"]["artifact_id"] == redacted.id
    assert report["redacted"]["sha256"] == redacted.sha256
    assert len(report["source"]["sha256"]) == 64


def test_every_rule_appears_even_when_it_matched_nothing(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """ "Was this masked?" needs an answer for rules that found nothing."""
    job_id = _run(conn, worker, _store_source(conn, tmp_path, "nothing to see here\n"))

    report = report_from_job(conn, tmp_path, job_id)
    assert [row["name"] for row in report["rules"]] == [rule.name for rule in RULES]
    assert report["total_hits"] == 0


def test_an_empty_file_is_redacted_rather_than_failing(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """A zero-byte source is the divide-by-zero in the progress calculation."""
    job_id = _run(conn, worker, _store_source(conn, tmp_path, ""))

    assert get_job(conn, job_id).state == SUCCEEDED
    assert report_from_job(conn, tmp_path, job_id)["lines"] == 0


def test_line_endings_survive_the_round_trip(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """A redacted log whose CRLFs were rewritten no longer matches its source.

    Read as bytes, because reading it as text would translate the newlines back
    and pass whatever the job actually wrote.
    """
    text = "host 10.20.30.41 here\r\nhost 10.20.30.42 there\r\n"
    job_id = _run(conn, worker, _store_source(conn, tmp_path, text))

    redacted = next(
        item for item in list_for_job(conn, job_id) if item.name == "xensource.redacted.log"
    )
    body = artifact_path(tmp_path, job_id, redacted.id).read_bytes()
    assert body == b"host [IPv4] here\r\nhost [IPv4] there\r\n"


def test_a_file_with_a_bad_byte_is_redacted_rather_than_failing(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Real logs contain malformed bytes; one must not lose the whole run."""
    _finished_source_job(conn)
    path = tmp_path / "incoming.log"
    path.write_bytes(b"host 10.20.30.41 \xff here\n")
    artifact = store_file(
        conn,
        tmp_path,
        job_id=SOURCE_JOB_ID,
        name="xensource.log",
        media_type="text/plain",
        source=path,
    )
    job_id = _run(conn, worker, artifact.id)

    assert get_job(conn, job_id).state == SUCCEEDED
    redacted = next(
        item for item in list_for_job(conn, job_id) if item.name == "xensource.redacted.log"
    )
    body = artifact_path(tmp_path, job_id, redacted.id).read_bytes()
    assert body == b"host [IPv4] \xff here\n"


def test_a_missing_artifact_fails_the_job_with_the_reason(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job = enqueue(conn, KIND, {"artifact_id": "no-such-artifact"})
    worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "no longer stored" in stored.error


def test_no_artifact_named_fails_the_job_with_its_own_reason(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """A different fix from a deleted artifact, so a different message."""
    job = enqueue(conn, KIND)
    worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "no artifact was named" in stored.error.lower()


def test_a_missing_body_fails_the_job_rather_than_storing_an_empty_copy(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """A row whose file is gone must not become a zero-byte "redacted" bundle."""
    artifact_id = _store_source(conn, tmp_path, SAMPLE)
    stored_artifact = get_artifact(conn, artifact_id)
    artifact_path(tmp_path, stored_artifact.job_id, stored_artifact.id).unlink()

    job = enqueue(conn, KIND, {"artifact_id": artifact_id})
    worker.run_one(conn)

    failed = get_job(conn, job.id)
    assert failed.state == FAILED
    assert "missing from the data volume" in failed.error
    assert list_for_job(conn, job.id) == []


def test_the_redacted_name_keeps_the_suffix_last() -> None:
    """A redacted .log must still open as a .log."""
    assert redacted_name("xensource.log") == "xensource.redacted.log"
    assert redacted_name("logs.tgz") == "logs.redacted.tgz"
    assert redacted_name("bundle") == "bundle.redacted"


def test_a_job_with_no_report_reads_as_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A job that failed before writing one must not raise on read."""
    job = enqueue(conn, KIND)
    assert report_from_job(conn, tmp_path, job.id) is None


def test_a_report_from_an_older_version_still_renders(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A rule added since the report was written is shown, not raised on.

    And a rule the report names that this version no longer has keeps its row,
    because dropping it would silently lose hits the run recorded.
    """
    job = enqueue(conn, KIND)
    store_json(
        conn,
        tmp_path,
        job_id=job.id,
        name=REPORT_ARTIFACT,
        payload={
            "rules": [
                {"name": "ipv4", "title": "IPv4 addresses", "placeholder": "[IPv4]", "hits": 3},
                {"name": "a_rule_we_dropped", "hits": 2},
            ]
        },
    )

    rows = report_rows(report_from_job(conn, tmp_path, job.id))
    by_name = {row["name"]: row for row in rows}

    assert by_name["ipv4"]["hits"] == 3
    # Present in RULES, absent from the stored report: shown with no count.
    assert by_name["uuid"]["hits"] == 0
    assert by_name["uuid"]["title"] == "UUIDs"
    # Present in the report, gone from RULES: kept, with its count.
    assert by_name["a_rule_we_dropped"]["hits"] == 2
