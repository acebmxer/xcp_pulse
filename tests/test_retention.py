"""Retention: what a cleanup would delete, and that it deletes only that.

The dangerous direction is deleting too much. A collection is a 433 MB,
100-second download, so an over-eager policy costs real time to undo — which is
why the preview and the deletion have to agree, and why the count limit has to
win over the age limit rather than the other way round.

The files are checked on disk, not only the rows: a row removed while its body
stays behind is invisible and never reclaimed, which is the failure that
silently fills a data volume.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app import retention
from app.artifacts import artifacts_dir, store_json
from app.db import init_db
from app.job_collect import KIND as COLLECT_KIND
from app.jobs import enqueue, get_job, mark_succeeded


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


def _collection(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    host: str,
    days_old: float = 0.0,
    kind: str = COLLECT_KIND,
) -> str:
    """A finished collection with one stored file, aged as asked.

    ``created_at`` and ``finished_at`` are written directly because the age
    that matters is days and no test is going to wait for one.
    """
    job = enqueue(conn, kind, {"host_id": f"id-{host}", "host_name": host})
    store_json(conn, data_dir, job_id=job.id, name=f"{host}-logs.tgz", payload={"host": host})
    mark_succeeded(conn, job.id)

    when = time.time() - days_old * 86400
    conn.execute(
        "UPDATE jobs SET created_at = ?, finished_at = ? WHERE id = ?",
        (when, when, job.id),
    )
    conn.commit()
    return job.id


def _hosts(items) -> list[str]:
    return [item.host_name for item in items]


def test_a_plan_deletes_nothing_when_everything_is_inside_the_limits(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _collection(conn, tmp_path, host="host-a", days_old=1)
    _collection(conn, tmp_path, host="host-b", days_old=2)

    plan = retention.plan(conn, keep_days=30, keep_count=3)
    assert plan.is_empty
    assert len(plan.keep) == 2


def test_the_count_limit_keeps_a_collection_the_age_limit_would_drop(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The rule that stops a long gap in collecting from emptying the store.

    Everything here is far past the age limit, but an operator who has not
    collected for a year still wants the last one they took.
    """
    _collection(conn, tmp_path, host="newest", days_old=100)
    _collection(conn, tmp_path, host="older", days_old=200)
    _collection(conn, tmp_path, host="oldest", days_old=300)

    plan = retention.plan(conn, keep_days=30, keep_count=2)
    assert _hosts(plan.keep) == ["newest", "older"]
    assert _hosts(plan.delete) == ["oldest"]


def test_the_age_limit_keeps_a_recent_collection_past_the_count(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Both limits have to be satisfied before something is deleted."""
    for host in ("a", "b", "c", "d"):
        _collection(conn, tmp_path, host=host, days_old=1)

    plan = retention.plan(conn, keep_days=30, keep_count=1)
    assert plan.is_empty


def test_a_zero_age_limit_keeps_only_the_newest_few(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The tightest policy an operator short of disk can ask for."""
    _collection(conn, tmp_path, host="newest", days_old=0)
    _collection(conn, tmp_path, host="middle", days_old=1)
    _collection(conn, tmp_path, host="oldest", days_old=2)

    plan = retention.plan(conn, keep_days=0, keep_count=1)
    assert _hosts(plan.keep) == ["newest"]
    assert _hosts(plan.delete) == ["middle", "oldest"]


def test_applying_deletes_the_files_as_well_as_the_rows(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A row removed while its body stays is space nothing ever reclaims."""
    kept = _collection(conn, tmp_path, host="newest", days_old=1)
    dropped = _collection(conn, tmp_path, host="oldest", days_old=90)

    retention.apply(conn, tmp_path, keep_days=30, keep_count=1)

    assert get_job(conn, dropped) is None
    assert get_job(conn, kept) is not None
    assert not (artifacts_dir(tmp_path) / dropped).exists()
    assert (artifacts_dir(tmp_path) / kept).exists()


def test_applying_deletes_exactly_what_the_preview_named(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The preview is the promise the button has to keep."""
    _collection(conn, tmp_path, host="newest", days_old=1)
    _collection(conn, tmp_path, host="oldest", days_old=90)

    previewed = _hosts(retention.plan(conn, keep_days=30, keep_count=1).delete)
    applied = _hosts(retention.apply(conn, tmp_path, keep_days=30, keep_count=1).delete)

    assert previewed == applied == ["oldest"]


def test_applying_reports_the_space_it_freed(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _collection(conn, tmp_path, host="oldest", days_old=90)

    applied = retention.apply(conn, tmp_path, keep_days=30, keep_count=0)
    assert applied.freed_bytes > 0


def test_only_collections_are_considered(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """An inventory refresh is not a collection and must not be swept up.

    It is a few hundred bytes the dashboard reads, and deleting it would empty
    the page for no gain in space.
    """
    other = _collection(conn, tmp_path, host="inventory", days_old=999, kind="refresh_inventory")

    plan = retention.plan(conn, keep_days=1, keep_count=0)
    assert plan.is_empty

    retention.apply(conn, tmp_path, keep_days=1, keep_count=0)
    assert get_job(conn, other) is not None


def test_a_failed_collection_is_not_offered_for_retention(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """It has no result worth counting, and its files went with the failure."""
    job = enqueue(conn, COLLECT_KIND, {"host_id": "x", "host_name": "x"})
    conn.execute("UPDATE jobs SET state = 'failed', finished_at = ? WHERE id = ?", (0.0, job.id))
    conn.commit()

    assert retention.collections(conn) == []


def test_deleting_one_collection_leaves_the_others_alone(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    first = _collection(conn, tmp_path, host="host-a", days_old=1)
    second = _collection(conn, tmp_path, host="host-b", days_old=1)

    assert retention.delete_collection(conn, tmp_path, first) is True

    assert get_job(conn, first) is None
    assert get_job(conn, second) is not None
    assert not (artifacts_dir(tmp_path) / first).exists()


def test_deleting_a_collection_that_is_gone_says_so_rather_than_raising(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    assert retention.delete_collection(conn, tmp_path, "no-such-job") is False


def test_a_plan_totals_what_is_stored_across_kept_and_deleted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """What the page shows above the preview."""
    _collection(conn, tmp_path, host="newest", days_old=1)
    _collection(conn, tmp_path, host="oldest", days_old=90)

    plan = retention.plan(conn, keep_days=30, keep_count=1)
    assert plan.stored_bytes == sum(item.size_bytes for item in plan.keep + plan.delete)
    assert plan.stored_bytes > plan.freed_bytes


def test_a_failed_collection_can_be_deleted(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The case an operator most wants the button for.

    A failed collection is exactly what someone wants to clear away, and
    looking it up through ``collections()`` — which lists only successful
    jobs — made the delete button silently do nothing.
    """
    job = enqueue(conn, COLLECT_KIND, {"host_id": "x", "host_name": "failed-host"})
    store_json(conn, tmp_path, job_id=job.id, name="partial.tgz", payload={})
    conn.execute(
        "UPDATE jobs SET state = 'failed', finished_at = ? WHERE id = ?", (time.time(), job.id)
    )
    conn.commit()

    assert retention.delete_collection(conn, tmp_path, job.id) is True
    assert get_job(conn, job.id) is None
    assert not (artifacts_dir(tmp_path) / job.id).exists()


def test_a_cancelled_collection_can_be_deleted(conn: sqlite3.Connection, tmp_path: Path) -> None:
    job = enqueue(conn, COLLECT_KIND, {"host_id": "x", "host_name": "cancelled-host"})
    conn.execute(
        "UPDATE jobs SET state = 'cancelled', finished_at = ? WHERE id = ?", (time.time(), job.id)
    )
    conn.commit()

    assert retention.delete_collection(conn, tmp_path, job.id) is True


def test_a_running_collection_is_not_deleted_from_under_the_worker(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Its files are still being written; removing them orphans the download."""
    job = enqueue(conn, COLLECT_KIND, {"host_id": "x", "host_name": "running-host"})
    conn.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (job.id,))
    conn.commit()

    assert retention.delete_collection(conn, tmp_path, job.id) is False
    assert get_job(conn, job.id) is not None


def test_a_job_of_another_kind_is_not_deleted_through_the_collect_page(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The id comes from a URL, so it must not reach an inventory refresh."""
    other = _collection(conn, tmp_path, host="inv", days_old=1, kind="refresh_inventory")

    assert retention.delete_collection(conn, tmp_path, other) is False
    assert get_job(conn, other) is not None
