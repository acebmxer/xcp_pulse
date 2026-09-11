"""The findings job: what it stores, and what reads it back.

The round trip matters more than either half. The page renders what this job
stored, so a job writing something the reader cannot rebuild would leave the
page empty with a job marked successful — the exact failure a stored result
exists to prevent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from app.artifacts import artifact_path, list_for_job
from app.db import init_db
from app.findings import CRITICAL, INFO, WARNING, Finding, Report, SourceResult
from app.job_findings import (
    FINDINGS_ARTIFACT,
    FINDINGS_MARKDOWN,
    KIND,
    report_from_job,
    to_markdown,
)
from app.job_runner import JobWorker
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job
from app.xo_client import XoError
from app.xo_connection import save_connection

SECRET = "test-secret-key-not-for-production"

FINDING = Finding(
    severity=CRITICAL,
    title="A storage repository backend failed",
    evidence="nfs mount from [IPV4] failed",
    action="Check the SR is attached.",
    source="messages",
    at=1788600000.0,
    count=3,
    object_id="sr-a",
)

REPORT = Report(
    findings=[FINDING],
    sources=[
        SourceResult("messages", read=True, examined=799),
        SourceResult("dashboard", read=False, reason="needs an administrator account"),
    ],
    window_days=30,
    created_at=1788700000.0,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "test.db")
    save_connection(
        conn,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=SECRET,
    )
    return conn


class _Settings:
    secret_key = SECRET


@pytest.fixture
def worker(tmp_path: Path) -> JobWorker:
    return JobWorker(tmp_path / "test.db", tmp_path, _Settings())


def _run(conn: sqlite3.Connection, worker: JobWorker, report: Report = REPORT) -> str:
    job = enqueue(conn, KIND)
    with (
        patch("app.job_findings.build_client"),
        patch("app.job_findings.collect_findings", return_value=report),
    ):
        worker.run_one(conn)
    return job.id


def test_both_copies_of_the_report_are_stored(conn: sqlite3.Connection, worker: JobWorker) -> None:
    """JSON for the page, Markdown for a support ticket."""
    job_id = _run(conn, worker)

    assert get_job(conn, job_id).state == SUCCEEDED
    names = sorted(item.name for item in list_for_job(conn, job_id))
    assert names == [FINDINGS_ARTIFACT, FINDINGS_MARKDOWN]


def test_what_is_stored_rebuilds_into_the_same_report(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The round trip the page depends on, field for field."""
    job_id = _run(conn, worker)

    rebuilt = report_from_job(conn, tmp_path, job_id)
    assert rebuilt.findings == [FINDING]
    assert rebuilt.window_days == 30
    assert rebuilt.created_at == 1788700000.0
    assert [source.name for source in rebuilt.unread_sources] == ["dashboard"]
    assert rebuilt.counts == {"critical": 1, "warning": 0, "info": 0}


def test_a_clean_report_is_stored_rather_than_treated_as_nothing(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """ "Nothing is wrong" is a result worth keeping, and worth showing."""
    job_id = _run(conn, worker, Report(sources=[SourceResult("messages", read=True)]))

    assert get_job(conn, job_id).state == SUCCEEDED
    rebuilt = report_from_job(conn, tmp_path, job_id)
    assert rebuilt.is_clean
    assert rebuilt.sources


def test_the_final_step_says_how_many_findings_there_were(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job_id = _run(conn, worker)

    step = get_job(conn, job_id).step
    assert "1 finding(s)" in step
    assert "1 critical" in step


def test_an_unreachable_xo_fails_the_job_with_the_reason(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job = enqueue(conn, KIND)
    with patch("app.job_findings.build_client", side_effect=XoError("cannot reach XO")):
        worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "cannot reach" in stored.error


def test_no_configured_connection_fails_the_job_with_its_own_reason(
    tmp_path: Path, worker: JobWorker
) -> None:
    """A different fix from an unreachable address, so a different message."""
    conn = init_db(tmp_path / "test.db")
    job = enqueue(conn, KIND)
    worker.run_one(conn)

    assert get_job(conn, job.id).state == FAILED


def test_a_report_from_a_job_that_stored_none_reads_as_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job = enqueue(conn, KIND)
    assert report_from_job(conn, tmp_path, job.id) is None


def test_a_report_written_by_an_older_version_still_loads(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Unknown keys are dropped, missing ones stay at their default.

    Without this, adding a field to Finding would make every stored report
    raise on read — turning an upgrade into an empty page.
    """
    job_id = _run(conn, worker)
    artifact = next(item for item in list_for_job(conn, job_id) if item.name == FINDINGS_ARTIFACT)
    path = artifact_path(tmp_path, job_id, artifact.id)
    path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "severity": "warning",
                        "title": "From an older version",
                        "evidence": "e",
                        "action": "a",
                        "source": "tasks",
                        "a_field_that_no_longer_exists": True,
                    }
                ],
                "sources": [{"name": "tasks", "read": True}],
            }
        )
    )

    rebuilt = report_from_job(conn, tmp_path, job_id)
    assert rebuilt.findings[0].title == "From an older version"
    assert rebuilt.findings[0].count == 1
    assert rebuilt.window_days == 30


def test_a_corrupt_report_reads_as_none_rather_than_raising(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    job_id = _run(conn, worker)
    artifact = next(item for item in list_for_job(conn, job_id) if item.name == FINDINGS_ARTIFACT)
    artifact_path(tmp_path, job_id, artifact.id).write_text("not json at all")

    assert report_from_job(conn, tmp_path, job_id) is None


# -- the markdown copy ---------------------------------------------------


def test_the_markdown_names_the_finding_its_evidence_and_its_action() -> None:
    text = to_markdown(REPORT)

    assert "# XCP Pulse: findings from the Xen Orchestra API" in text
    assert "## Critical" in text
    assert "### A storage repository backend failed (3 times)" in text
    assert "nfs mount from [IPV4] failed" in text
    assert "**What to do:** Check the SR is attached." in text


def test_the_markdown_says_which_sources_could_not_be_read() -> None:
    """A report missing a source has not checked what that source covers."""
    text = to_markdown(REPORT)

    assert "## Sources" in text
    assert "**not read**: needs an administrator account" in text
    assert "| XAPI messages | XCP-ng hosts |" in text
    assert "799 messages" in text


def test_the_markdown_says_whether_a_source_is_the_hosts_or_xen_orchestra() -> None:
    """XO serves all seven routes but originates only three of them.

    Which it is decides where an operator goes to act: a XAPI message means log
    in to the host, a failed task means look in Xen Orchestra.
    """
    text = to_markdown(REPORT)

    assert "| Source | Comes from | What it holds | Result |" in text
    assert "| XAPI messages | XCP-ng hosts |" in text
    assert "| Pool dashboard | XCP-ng hosts |" in text


def test_a_clean_report_says_so_rather_than_being_an_empty_document() -> None:
    text = to_markdown(Report(sources=[SourceResult("messages", read=True)]))

    assert "No findings." in text
    assert "0 critical, 0 warning, 0 informational" in text


def test_severities_appear_as_headings_in_order() -> None:
    report = Report(
        findings=[
            Finding(CRITICAL, "C", "e", "a", "messages"),
            Finding(WARNING, "W", "e", "a", "tasks"),
            Finding(INFO, "I", "e", "a", "dashboard"),
        ]
    )
    text = to_markdown(report)

    assert text.index("## Critical") < text.index("## Warning") < text.index("## Info")


def test_a_pipe_in_a_reason_does_not_break_the_sources_table() -> None:
    """The reason comes from XO, so it can contain anything."""
    report = Report(sources=[SourceResult("tasks", read=False, reason="a | b")])
    text = to_markdown(report)

    assert "a \\| b" in text


def test_the_stored_markdown_is_the_same_text(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The file on disk and the page are built from one report, not two."""
    job_id = _run(conn, worker)
    artifact = next(item for item in list_for_job(conn, job_id) if item.name == FINDINGS_MARKDOWN)

    stored = artifact_path(tmp_path, job_id, artifact.id).read_text(encoding="utf-8")
    assert stored == to_markdown(REPORT)
    assert artifact.media_type == "text/markdown"


def test_no_staging_file_is_left_behind(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    job_id = _run(conn, worker)

    leftovers = list((tmp_path / "artifacts").glob("*.tmp"))
    assert leftovers == []
    assert len(list_for_job(conn, job_id)) == 2


def test_switched_off_rules_survive_the_round_trip(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The page reads this back, so it has to be stored, not only computed."""
    partial = Report(sources=[SourceResult("messages", read=True)], rules_disabled=["UUIDs"])
    job_id = _run(conn, worker, partial)

    assert report_from_job(conn, tmp_path, job_id).rules_disabled == ["UUIDs"]


def test_the_markdown_warns_before_the_findings_that_masking_was_partial() -> None:
    """Someone pasting this into a ticket sees it while they can still stop."""
    report = Report(
        findings=[Finding(WARNING, "Something", "evidence", "act", "tasks")],
        rules_disabled=["UUIDs", "IPv4 addresses"],
    )
    text = to_markdown(report)

    assert "2 redaction rule(s) were switched off" in text
    assert "UUIDs, IPv4 addresses" in text
    assert text.index("switched off") < text.index("### Something")


def test_the_markdown_says_nothing_about_rules_when_they_were_all_on() -> None:
    """The other side: a clean report must not carry a masking warning."""
    assert "switched off" not in to_markdown(REPORT)


def test_the_markdown_is_pure_ascii() -> None:
    """A findings report is emailed, pasted and opened by other tools.

    An em dash is three UTF-8 bytes, and anything that opens the file as
    Latin-1 renders it as "â" — which is what happened to a real report: the
    file on disk was valid UTF-8 and the download header said charset=utf-8,
    and it still arrived mojibaked. The reliable fix is to emit nothing that
    can break that way, so this holds the whole document to ASCII.

    Evidence is exempt in principle because it comes from Xen Orchestra, but
    everything XCP Pulse writes around it is ours to control.
    """
    report = Report(
        findings=[
            Finding(CRITICAL, "A title", "evidence", "an action", "messages", count=3),
            Finding(INFO, "Another", "more evidence", "act", "dashboard"),
        ],
        sources=[
            SourceResult("messages", read=True, examined=799),
            SourceResult("alarms", read=True, examined=0),
            SourceResult(
                "patches", read=True, examined=0, detail="none missing - Pool1 up to date"
            ),
            SourceResult("dashboard", read=False, reason="needs an administrator account"),
        ],
        rules_disabled=["UUIDs"],
    )

    text = to_markdown(report)
    offenders = sorted({char for char in text if ord(char) > 127})

    assert offenders == [], f"non-ASCII in the generated report: {offenders}"
    text.encode("ascii")


def test_a_repeat_count_reads_without_a_multiplication_sign() -> None:
    """The × that made "(3×)" was one of the characters that mojibaked."""
    report = Report(findings=[Finding(CRITICAL, "A title", "e", "a", "messages", count=3)])

    assert "(3 times)" in to_markdown(report)
