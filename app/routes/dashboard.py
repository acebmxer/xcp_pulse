"""The landing page after logging in.

Shows the pools and hosts from the last successful **Refresh inventory** job
rather than calling Xen Orchestra on every page load. What that buys is not
speed — these routes answer in milliseconds — but that the inventory is a
stored result with a time attached: the page says how old it is, and an XO that
is unreachable leaves the last known inventory on screen instead of an error
where the hosts were.

The first load after configuring a connection has no stored result yet, so one
refresh is queued automatically. Only the first: after that, refreshing is
something the operator asks for on the jobs page.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, templates
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_inventory import inventory_from_job
from app.jobs import enqueue, has_active, latest_job, latest_successful
from app.xo_client import Inventory
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.dashboard")

# Shown when XO answers normally but the account can see nothing. Worth saying
# in full because an empty list is what XO returns for missing privileges as
# well as for an empty installation, and the two look identical from here.
EMPTY_HELP = (
    "Xen Orchestra answered, but this account can see no pools or hosts. "
    "Xen Orchestra returns an empty list rather than an error when an account "
    "lacks read access, so this usually means the account needs pool and host "
    "read privileges rather than that there is nothing installed."
)

# Shown while the very first refresh is still running, when there is no earlier
# result to fall back on.
PENDING_HELP = (
    "Reading the inventory from Xen Orchestra. This page will show your pools "
    "and hosts once it finishes."
)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, username: str = Depends(login_required)) -> Response:
    db = request.app.state.db
    settings = request.app.state.settings
    connection = get_connection(db)

    inventory: Inventory | None = None
    result_job = None
    error: str | None = None

    if connection is not None:
        result_job = latest_successful(db, INVENTORY_KIND)
        if result_job is not None:
            inventory = inventory_from_job(db, settings.data_dir, result_job.id)

        # The newest attempt, which may be a failure sitting after a success.
        # Its error is worth showing even when there is still a usable
        # inventory underneath, because the numbers on screen are then stale.
        newest = latest_job(db, INVENTORY_KIND)
        if newest is not None and newest.error:
            error = newest.error

        if newest is None and not has_active(db, INVENTORY_KIND):
            # A connection is configured but nothing has ever read it — the
            # first load after saving one. Queue the refresh rather than making
            # the operator find the button to see anything at all.
            enqueue(db, INVENTORY_KIND)
            _wake_worker(request)
            log.info("queued the first %s job", INVENTORY_KIND)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "username": username,
            "connection": connection,
            "inventory": inventory,
            "result_job": result_job,
            "refreshing": has_active(db, INVENTORY_KIND) if connection else False,
            "error": error,
            "empty_help": EMPTY_HELP,
            "pending_help": PENDING_HELP,
        },
    )


def _wake_worker(request: Request) -> None:
    """Tell the worker to look now rather than at its next poll.

    Deliberately not app-state-specific: absent when jobs run in a separate
    process, where the queue in the database is the only handover needed.
    """
    worker = getattr(request.app.state, "job_worker", None)
    if worker is not None:
        worker.wake()
