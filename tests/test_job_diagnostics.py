"""The "Collect XO diagnostics" job: what it stores, and what reads it back."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from app.artifacts import list_for_job
from app.db import init_db
from app.job_diagnostics import (
    BACKUP_RESTORE_ARTIFACT,
    KIND,
    MESSAGES_ALARMS_ARTIFACT,
    REPORT_ARTIFACT,
    SOURCE_BACKUP_RESTORE,
    SOURCE_MESSAGES_ALARMS,
    SOURCE_TASKS,
    TASKS_ARTIFACT,
    diagnostics_artifacts_from_job,
    report_from_job,
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


def _run(
    conn: sqlite3.Connection, worker: JobWorker, client: _FakeClient, sources=None, **extra
) -> str:
    job = enqueue(conn, KIND, {"sources": sources or list(_ALL_SOURCES), **extra})
    with patch("app.job_diagnostics.build_client", return_value=client):
        worker.run_one(conn)
    return job.id


_ALL_SOURCES = (SOURCE_BACKUP_RESTORE, SOURCE_TASKS, SOURCE_MESSAGES_ALARMS)


def test_only_the_ticked_sources_are_stored(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    job_id = _run(conn, worker, _FakeClient(), sources=[SOURCE_TASKS])

    names = {item.name for item in list_for_job(conn, job_id)}
    assert TASKS_ARTIFACT in names
    assert BACKUP_RESTORE_ARTIFACT not in names
    assert MESSAGES_ALARMS_ARTIFACT not in names


def test_each_ticked_source_stores_a_raw_and_a_redacted_copy(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job_id = _run(conn, worker, _FakeClient())

    names = {item.name for item in list_for_job(conn, job_id)}
    assert names == {
        BACKUP_RESTORE_ARTIFACT,
        "backup-restore-detail.redacted.json",
        TASKS_ARTIFACT,
        "xapi-tasks.redacted.json",
        MESSAGES_ALARMS_ARTIFACT,
        "messages-alarms.redacted.json",
        REPORT_ARTIFACT,
    }


def test_no_sources_selected_fails_the_job(conn: sqlite3.Connection, worker: JobWorker) -> None:
    job = enqueue(conn, KIND, {"sources": []})
    with patch("app.job_diagnostics.build_client", return_value=_FakeClient()):
        worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "No diagnostics source" in stored.error


def test_detail_is_fetched_for_every_run_not_just_failures(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The rebuilt Stage 2 differs from the earlier wrong build here: this is
    an archive feature, so every enumerated run gets its detail fetched, not
    only ones that already failed."""
    client = _FakeClient(
        backups=[
            {"id": "b1", "jobName": "Nightly", "status": "success", "end": 1788600000000},
            {"id": "b2", "jobName": "Weekly", "status": "failure", "end": 1788600000000},
        ],
        detail={"b1": {"tasks": ["ok"]}, "b2": {"tasks": ["snapshot failed"]}},
    )
    job_id = _run(conn, worker, client, sources=[SOURCE_BACKUP_RESTORE])

    payload = report_from_job(conn, tmp_path, job_id)
    source = next(item for item in payload["sources"] if item["name"] == SOURCE_BACKUP_RESTORE)
    assert source["detail_count"] == 2


def test_a_run_whose_detail_fetch_fails_still_stores_the_rest_of_the_batch(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    client = _FakeClient(
        restores=[{"id": "r1", "jobName": "Restore test", "status": "error", "end": 1788600000000}]
    )
    job_id = _run(conn, worker, client, sources=[SOURCE_BACKUP_RESTORE])

    payload = report_from_job(conn, tmp_path, job_id)
    source = next(item for item in payload["sources"] if item["name"] == SOURCE_BACKUP_RESTORE)
    assert source["read"] is True
    assert source["count"] == 1
    assert source["detail_count"] == 0
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
    job_id = _run(conn, worker, client, sources=[SOURCE_TASKS])

    from app.artifacts import read_json

    raw = next(item for item in list_for_job(conn, job_id) if item.name == TASKS_ARTIFACT)
    redacted = next(
        item for item in list_for_job(conn, job_id) if item.name == "xapi-tasks.redacted.json"
    )
    assert "opsadmin" in str(read_json(tmp_path, raw))
    assert "[USER]" in str(read_json(tmp_path, redacted))
    assert "opsadmin" not in str(read_json(tmp_path, redacted))


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
    source = next(item for item in payload["sources"] if item["name"] == SOURCE_TASKS)
    assert source["read"] is False
    assert source["reason"] == "no permission"
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
    job = enqueue(
        conn,
        KIND,
        {
            "sources": [SOURCE_MESSAGES_ALARMS],
            "date_start": "2000-01-01",
            "date_end": "2000-01-02",
        },
    )
    with patch("app.job_diagnostics.build_client", return_value=client):
        worker.run_one(conn)

    from app.artifacts import read_json

    artifact = next(
        item for item in list_for_job(conn, job.id) if item.name == MESSAGES_ALARMS_ARTIFACT
    )
    stored = read_json(tmp_path, artifact)
    ids = [record["id"] for record in stored["messages"]]
    assert ids == ["m1"]


def test_disabled_rules_are_recorded(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    set_enabled_rules(conn, {"mac", "hostname"})
    job_id = _run(conn, worker, _FakeClient())

    payload = report_from_job(conn, tmp_path, job_id)
    assert "IPv4 addresses" in payload["rules_disabled"]


def test_an_unreachable_xo_fails_the_job(conn: sqlite3.Connection, worker: JobWorker) -> None:
    job = enqueue(conn, KIND, {"sources": list(_ALL_SOURCES)})
    with patch("app.job_diagnostics.build_client", side_effect=XoError("cannot reach XO")):
        worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "cannot reach" in stored.error


def test_a_report_from_a_job_that_stored_none_reads_as_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job = enqueue(conn, KIND, {"sources": list(_ALL_SOURCES)})
    assert report_from_job(conn, tmp_path, job.id) is None


def test_a_failed_run_gets_its_headline_extracted_into_the_report(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Reported from a real run: without this, an operator who downloads the
    report and the full detail archive still has to find the failure by eye
    inside a multi-hundred-KiB nested JSON tree."""
    client = _FakeClient(
        backups=[
            {"id": "b1", "jobName": "Nightly", "status": "success", "end": 1788600000000},
            {"id": "b2", "jobName": "Delta Backup", "status": "failure", "end": 1788600000000},
        ],
        detail={
            "b1": {"result": {"message": "ok"}},
            "b2": {"message": "backup", "result": {"message": "couldn't instantiate any remote"}},
        },
    )
    job_id = _run(conn, worker, client, sources=[SOURCE_BACKUP_RESTORE])

    payload = report_from_job(conn, tmp_path, job_id)
    assert len(payload["failures"]) == 1
    failure = payload["failures"][0]
    assert failure["job_name"] == "Delta Backup"
    assert failure["status"] == "failure"
    assert failure["headline"] == "couldn't instantiate any remote"


def test_a_failed_run_with_no_recognisable_shape_is_still_listed_with_no_headline(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    client = _FakeClient(
        restores=[{"id": "r1", "jobName": "Restore test", "status": "error", "end": 1788600000000}],
        detail={"r1": {"message": "restore"}},
    )
    job_id = _run(conn, worker, client, sources=[SOURCE_BACKUP_RESTORE])

    payload = report_from_job(conn, tmp_path, job_id)
    assert payload["failures"] == [
        {"job_name": "Restore test", "status": "error", "headline": None}
    ]


def test_diagnostics_artifacts_from_job_returns_only_the_redacted_copies(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job_id = _run(conn, worker, _FakeClient())

    names = {item.name for item in diagnostics_artifacts_from_job(conn, job_id)}
    assert names == {
        "backup-restore-detail.redacted.json",
        "xapi-tasks.redacted.json",
        "messages-alarms.redacted.json",
    }
    assert all(".redacted." in name for name in names)
