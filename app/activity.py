"""The activity log: a record of who did what, for admins and operators to
review.

One helper, called from every route that changes state — login/logout, XO
connection settings, redaction rules, a job started, an artifact deleted, a
user added/edited. A second copy of this logic at each call site would mean a
call site added later silently isn't logged; funnelling everything through one
function means the only way to miss an action is to forget the one call.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ActivityEntry:
    id: int
    username: str
    action: str
    detail: str
    ip: str | None
    created_at: float


def log_activity(
    conn: sqlite3.Connection, username: str, action: str, detail: str = "", ip: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO activity_log (username, action, detail, ip, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, action, detail, ip, time.time()),
    )
    conn.commit()


def list_activity(conn: sqlite3.Connection, limit: int = 200) -> list[ActivityEntry]:
    """Most recent entries first, capped at ``limit`` — this is a review page,
    not an export. Nothing prunes old rows yet; a growing table is cheap
    (short text rows, not the multi-hundred-MB artifacts app/retention.py
    manages) but if it ever needs a cap, that belongs there too.
    """
    rows = conn.execute(
        "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        ActivityEntry(
            id=row["id"],
            username=row["username"],
            action=row["action"],
            detail=row["detail"],
            ip=row["ip"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
