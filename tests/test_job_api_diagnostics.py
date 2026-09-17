"""The "Collect XO diagnostics" job: what it stores, and what reads it back.

Mirrors ``test_job_findings.py`` — the round trip matters more than either
half, because the page renders what this job stored.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from app.artifacts import list_for_job
from app.db import init_db
from app.job_api_diagnostics import (
    DIAGNOSTICS_ARTIFACT,
    DIAGNOSTICS_MARKDOWN,
    KIND,
    pretty_detail,
    report_from_job,
    run_headline,
    to_markdown,
)
from app.job_runner import JobWorker
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job
from app.redact import set_enabled_rules
from app.xo_client import XoError
from app.xo_connection import save_connection

SECRET = "test-secret-key-not-for-production"


class _FakeClient:
    """Enough of ``XoClient`` for the job body to run against."""

    def __init__(
        self,
        *,
        backups=None,
        restores=None,
        tasks=None,
        messages=None,
        alarms=None,
        users=None,
        detail=None,
        fail_users=False,
    ):
        self._backups = backups or []
        self._restores = restores or []
        self._tasks = tasks or []
        self._messages = messages or []
        self._alarms = alarms or []
        self._users = users or []
        self._detail = detail or {}
        self._fail_users = fail_users

    def users(self):
        if self._fail_users:
            raise XoError("cannot list users")
        return self._users

    def backup_logs(self, since):
        return self._backups

    def restore_logs(self, since):
        return self._restores

    def tasks(self, since):
        return self._tasks

    def messages(self, since):
        return self._messages

    def alarms(self, since):
        return self._alarms

    def backup_log_detail(self, log_id):
        if log_id not in self._detail:
            raise XoError(f"no detail for {log_id}")
        return self._detail[log_id]

    def restore_log_detail(self, log_id):
        return self.backup_log_detail(log_id)


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


def _run(conn: sqlite3.Connection, worker: JobWorker, client: _FakeClient) -> str:
    job = enqueue(conn, KIND)
    with patch("app.job_api_diagnostics.build_client", return_value=client):
        worker.run_one(conn)
    return job.id


def test_both_copies_are_stored(conn: sqlite3.Connection, worker: JobWorker) -> None:
    job_id = _run(conn, worker, _FakeClient())

    assert get_job(conn, job_id).state == SUCCEEDED
    names = sorted(item.name for item in list_for_job(conn, job_id))
    assert names == [DIAGNOSTICS_ARTIFACT, DIAGNOSTICS_MARKDOWN]


def test_a_failed_backup_gets_its_detail_fetched(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    client = _FakeClient(
        backups=[
            {"id": "b1", "jobName": "Nightly", "status": "failure", "end": 1788600000000},
            {"id": "b2", "jobName": "Weekly", "status": "success", "end": 1788600000000},
        ],
        detail={"b1": {"tasks": ["snapshot failed"]}},
    )
    job_id = _run(conn, worker, client)

    payload = report_from_job(conn, tmp_path, job_id)
    backups = {record["id"]: record for record in payload["backups"]}
    assert backups["b1"]["detail"] == {"tasks": ["snapshot failed"]}
    assert "detail" not in backups["b2"]


def test_a_run_whose_detail_fetch_fails_still_stores_the_summary(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    client = _FakeClient(
        restores=[{"id": "r1", "jobName": "Restore test", "status": "error", "end": 1788600000000}]
    )
    job_id = _run(conn, worker, client)

    payload = report_from_job(conn, tmp_path, job_id)
    assert payload["restores"][0]["detail_error"] == "no detail for r1"
    assert get_job(conn, job_id).state == SUCCEEDED


def test_usernames_are_masked_using_the_live_account_list(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """A plain (non-email-shaped) account name, so only the username rule —
    not the email rule, which would otherwise mask an email-shaped one first
    — is what is being exercised here."""
    client = _FakeClient(
        users=[{"id": "u1", "email": "opsadmin", "permission": "admin"}],
        tasks=[{"id": "t1", "properties": {"name": "opsadmin logged in"}, "end": 1788600000000}],
    )
    job_id = _run(conn, worker, client)

    payload = report_from_job(conn, tmp_path, job_id)
    assert "opsadmin" not in str(payload["tasks"])
    assert "[USER]" in str(payload["tasks"])


def test_a_users_failure_does_not_fail_the_job(conn: sqlite3.Connection, worker: JobWorker) -> None:
    job_id = _run(conn, worker, _FakeClient(fail_users=True))

    assert get_job(conn, job_id).state == SUCCEEDED


def test_a_source_that_cannot_be_read_is_recorded_rather_than_failing_the_job(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    client = _FakeClient()
    client.tasks = lambda since: (_ for _ in ()).throw(XoError("no permission"))
    job_id = _run(conn, worker, client)

    payload = report_from_job(conn, tmp_path, job_id)
    category = next(c for c in payload["categories"] if c["name"] == "tasks")
    assert category["read"] is False
    assert category["reason"] == "no permission"
    assert get_job(conn, job_id).state == SUCCEEDED


def test_an_upper_bound_end_date_excludes_later_records(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    client = _FakeClient(
        messages=[
            {"id": "m1", "name": "old", "time": 1000},
            {"id": "m2", "name": "new", "time": 9999999999},
        ]
    )
    job = enqueue(conn, KIND, {"date_start": "2000-01-01", "date_end": "2000-01-02"})
    with patch("app.job_api_diagnostics.build_client", return_value=client):
        worker.run_one(conn)

    payload = report_from_job(conn, tmp_path, job.id)
    ids = [record["id"] for record in payload["messages"]]
    assert ids == ["m1"]


def test_disabled_rules_are_recorded(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    set_enabled_rules(conn, {"mac", "hostname"})
    job_id = _run(conn, worker, _FakeClient())

    payload = report_from_job(conn, tmp_path, job_id)
    assert "IPv4 addresses" in payload["rules_disabled"]


def test_an_unreachable_xo_fails_the_job(conn: sqlite3.Connection, worker: JobWorker) -> None:
    job = enqueue(conn, KIND)
    with patch("app.job_api_diagnostics.build_client", side_effect=XoError("cannot reach XO")):
        worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "cannot reach" in stored.error


def test_a_report_from_a_job_that_stored_none_reads_as_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job = enqueue(conn, KIND)
    assert report_from_job(conn, tmp_path, job.id) is None


def test_no_staging_file_is_left_behind(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    _run(conn, worker, _FakeClient())

    leftovers = list((tmp_path / "artifacts").glob("*.tmp"))
    assert leftovers == []


# -- the markdown copy ---------------------------------------------------


def test_the_markdown_is_pure_ascii() -> None:
    payload = {
        "created_at": 1788700000.0,
        "categories": [
            {"name": "backups", "read": True, "count": 2, "detail_count": 1},
            {"name": "tasks", "read": False, "reason": "not permitted"},
        ],
        "backups": [{"id": "b1", "jobName": "Nightly", "status": "failure", "detail": {"a": "b"}}],
        "restores": [],
        "tasks": [],
        "messages": [],
        "alarms": [],
        "rules_disabled": ["UUIDs"],
    }
    text = to_markdown(payload)
    offenders = sorted({char for char in text if ord(char) > 127})

    assert offenders == [], f"non-ASCII in the generated report: {offenders}"
    text.encode("ascii")
    assert "## Sources" in text
    assert "**not read**: not permitted" in text
    assert "## Failed backup and restore runs" in text
    assert "### Nightly (failure)" in text


def test_the_markdown_pretty_prints_detail_instead_of_a_python_repr() -> None:
    """A real failure's detail used to render as ``{'a': 'b', ...}`` with
    literal ``\\n`` in stack traces — this is what replaced it."""
    payload = {
        "created_at": 1788700000.0,
        "categories": [],
        "backups": [
            {
                "id": "b1",
                "jobName": "Delta Backup",
                "status": "failure",
                "detail": {"result": {"message": "couldn't instantiate any remote"}},
            }
        ],
        "restores": [],
        "tasks": [],
        "messages": [],
        "alarms": [],
        "rules_disabled": [],
    }
    text = to_markdown(payload)

    assert "**couldn't instantiate any remote**" in text
    assert '"message": "couldn\'t instantiate any remote"' in text
    assert "{'result':" not in text


# -- run_headline and pretty_detail --------------------------------------


def test_run_headline_finds_the_nested_result_message() -> None:
    detail = {"message": "backup", "result": {"message": "couldn't instantiate any remote"}}

    assert run_headline(detail) == "couldn't instantiate any remote"


def test_run_headline_is_none_when_the_shape_is_not_there() -> None:
    assert run_headline({"message": "backup"}) is None
    assert run_headline({"result": "not a dict"}) is None
    assert run_headline("not even a dict") is None
    assert run_headline(None) is None


def test_pretty_detail_is_indented_json_not_a_python_repr() -> None:
    text = pretty_detail({"a": "b"})

    assert '"a": "b"' in text
    assert "  " in text
    assert "'a'" not in text


def test_pretty_detail_keeps_embedded_newlines_as_valid_json_escapes() -> None:
    """The JSON shape is left exactly as XO returns it — a multi-line value
    stays one valid JSON string with a literal ``\\n`` in it, rather than
    being rewritten into something no longer valid JSON."""
    text = pretty_detail({"stack": "Error: boom\n    at foo"})

    assert '"Error: boom\\n    at foo"' in text
    json.loads(text)
