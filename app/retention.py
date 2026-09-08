"""Retention: what to delete, shown before anything is deleted.

A collection stores about 870 MB — a 433 MB log bundle and a redacted copy —
or about 2.3 GiB when the optional audit trail is included. A handful of them
fills a data volume, and the
failure that matters is a full disk mid-download — a job that fails after 90
seconds with a half-written file, on the machine the operator was relying on.

So this exists to be *read* as much as run. Every caller asks
``plan`` first, which names exactly which collections would go and how much
space that returns, and the page shows that before offering the button. There
is no cleanup that happens without having been previewed.

Two limits, applied together, because they answer different questions:

* **age** — "I do not need collections older than a fortnight";
* **count** — "keep the last three, whatever their age".

A collection older than the age limit is kept anyway when dropping it would
take the count below the minimum, because an operator who has not collected
for a month still wants the last one they took.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from app.artifacts import Artifact, delete_for_job, list_for_job
from app.job_collect import KIND as COLLECT_KIND
from app.jobs import QUEUED, RUNNING, SUCCEEDED, Job, list_jobs

# Defaults. Deliberately generous: deleting a collection the operator still
# needed costs them another 100-second download, while keeping one too long
# costs disk that a preview makes visible.
DEFAULT_KEEP_DAYS = 30
DEFAULT_KEEP_COUNT = 3

# How far back the list of candidates reaches. Large enough that a data volume
# holding months of collections is fully considered.
_SCAN_LIMIT = 500


@dataclass(frozen=True)
class Collection:
    """One completed collection, as retention sees it."""

    job: Job
    artifacts: list[Artifact]

    @property
    def size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.artifacts)

    @property
    def host_name(self) -> str:
        """The host this collected from, for naming it on the page."""
        name = self.job.params.get("host_name") or self.job.params.get("host_id") or ""
        return str(name) or "unknown host"

    @property
    def age_days(self) -> float:
        finished = self.job.finished_at or self.job.created_at
        return max(0.0, (time.time() - finished) / 86400)


@dataclass(frozen=True)
class Plan:
    """What a cleanup would do, before it does it."""

    delete: list[Collection]
    keep: list[Collection]
    keep_days: int
    keep_count: int

    @property
    def freed_bytes(self) -> int:
        return sum(item.size_bytes for item in self.delete)

    @property
    def stored_bytes(self) -> int:
        return sum(item.size_bytes for item in self.delete + self.keep)

    @property
    def is_empty(self) -> bool:
        return not self.delete


def collections(conn: sqlite3.Connection) -> list[Collection]:
    """Every stored collection, newest first.

    Only successful jobs: a failed one has nothing worth keeping, and a running
    one is being written to as this reads.
    """
    jobs = [
        job
        for job in list_jobs(conn, kind=COLLECT_KIND, limit=_SCAN_LIMIT)
        if job.state == SUCCEEDED
    ]
    return [Collection(job=job, artifacts=list_for_job(conn, job.id)) for job in jobs]


def plan(
    conn: sqlite3.Connection,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    keep_count: int = DEFAULT_KEEP_COUNT,
) -> Plan:
    """Work out what a cleanup would delete, without deleting anything.

    The two limits are applied in order: the newest ``keep_count`` collections
    are kept whatever their age, and of the rest only those older than
    ``keep_days`` are dropped. That ordering is what stops a long gap in
    collecting from emptying the store.
    """
    keep_days = max(0, keep_days)
    keep_count = max(0, keep_count)

    stored = collections(conn)
    delete: list[Collection] = []
    keep: list[Collection] = []

    for index, item in enumerate(stored):
        if index < keep_count:
            keep.append(item)
        elif keep_days and item.age_days < keep_days:
            keep.append(item)
        else:
            # Reached either by being older than the age limit, or by
            # keep_days being zero — which means "keep only the newest
            # keep_count", the tightest policy an operator short of disk can
            # ask for.
            delete.append(item)

    return Plan(delete=delete, keep=keep, keep_days=keep_days, keep_count=keep_count)


def apply(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    keep_count: int = DEFAULT_KEEP_COUNT,
) -> Plan:
    """Delete what ``plan`` names, and return the plan that was applied.

    Re-planning here rather than taking a plan as an argument: a plan built for
    a page load and posted back minutes later could name a collection that has
    since been deleted, or miss one that has since aged out. The page shows a
    preview; this decides afresh at the moment it acts.

    Returning the plan is what lets the caller say what actually went, rather
    than repeating what the preview predicted.
    """
    applied = plan(conn, keep_days=keep_days, keep_count=keep_count)
    for item in applied.delete:
        # Artifacts first, then the job row: delete_for_job removes the files,
        # and a job row removed first would take its artifact rows with it by
        # cascade and leave the files behind, unreferenced and unreclaimable.
        delete_for_job(conn, data_dir, item.job.id)
        conn.execute("DELETE FROM jobs WHERE id = ?", (item.job.id,))
    conn.commit()
    return applied


def delete_job(
    conn: sqlite3.Connection,
    data_dir: Path,
    job_id: str,
    *,
    kind: str | None = None,
) -> bool:
    """Delete one job and its files outright. True when there was one to delete.

    Separate from the policy above because "this one, now" is a different
    question from "everything past the limits", and an operator who has just
    sent a bundle to Vates wants the first one.

    ``kind`` restricts what a page may delete, so the collect page cannot be
    made to remove a redaction and vice versa; ``None`` accepts any kind.

    Deliberately not ``collections()``: that lists only *successful* jobs,
    because a failed one has no result to retain — but a failed job is
    precisely what an operator most wants to clear away, and looking it up
    there made the button silently do nothing. Only an active job is refused,
    since deleting the files a running download is still writing would leave
    the worker writing to a path nothing owns.
    """
    sql = "SELECT id FROM jobs WHERE id = ? AND state NOT IN (?, ?)"
    params: list[object] = [job_id, QUEUED, RUNNING]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)

    row = conn.execute(sql, params).fetchone()
    if row is None:
        return False
    delete_for_job(conn, data_dir, job_id)
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    return True


def delete_collection(conn: sqlite3.Connection, data_dir: Path, job_id: str) -> bool:
    """Delete one collection outright. True when there was one to delete."""
    return delete_job(conn, data_dir, job_id, kind=COLLECT_KIND)
