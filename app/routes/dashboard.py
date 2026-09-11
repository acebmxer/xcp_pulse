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

The panels beneath the inventory follow the same rule: each reads a stored
result and links to the page that owns it, so the dashboard answers "is
anything wrong, and did the last run work?" without becoming a second copy of
Findings or Jobs. Nothing here calls Xen Orchestra.

**As features ship, they earn a panel only if they change that answer.** A
feature having a page is not a reason to summarise it here — a dashboard that
lists everything is one nobody reads. The test is whether an operator who
opened this page and nothing else would be missing something they needed to
act on.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app import retention
from app.artifacts import human_bytes
from app.dependencies import login_required, templates, wake_worker
from app.findings import SEVERITIES
from app.job_findings import KIND as FINDINGS_KIND
from app.job_findings import report_from_job
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_inventory import inventory_from_job
from app.jobs import enqueue, has_active, latest_job, latest_successful, list_jobs
from app.redact import RULES, enabled_rules
from app.xo_client import Inventory
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.dashboard")

# Shown when XO answered 200 with an empty list. That is now the only way to
# reach this text — a refused or failed read raises and lands on the job as an
# error instead — but 200 [] still has two causes XO does not distinguish, so
# this offers both rather than asserting the likelier one.
EMPTY_HELP = (
    "Xen Orchestra answered, but this account can see no pools or hosts. "
    "Either the account needs pool and host read privileges, or there is "
    "nothing registered in this Xen Orchestra — it returns an empty list for "
    "both, so they cannot be told apart from here. Refresh from the jobs page "
    "to read it again."
)

# Shown while the very first refresh is still running, when there is no earlier
# result to fall back on.
PENDING_HELP = (
    "Reading the inventory from Xen Orchestra. This page will show your pools "
    "and hosts once it finishes."
)

# How many recent jobs the activity panel shows. Deliberately small: this
# answers "did the last thing I started work?", and the jobs page is where a
# history is read.
RECENT_JOBS = 5


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
            wake_worker(request)
            log.info("queued the first %s job", INVENTORY_KIND)

    # The latest findings report, for the severity summary. Read from the
    # stored artifact the Findings page renders, so the two cannot disagree.
    findings_job = latest_successful(db, FINDINGS_KIND)
    report = report_from_job(db, settings.data_dir, findings_job.id) if findings_job else None

    # Rules switched off, by title. A bundle or report produced with masking
    # disabled is the failure worth seeing before it is sent, so it belongs on
    # the page an operator opens first rather than only on Redaction.
    enabled = enabled_rules(db)
    rules_off = [rule.title for rule in RULES if rule.name not in enabled]

    # Storage is the retention plan on its defaults — the same figures the
    # collect page shows, without repeating its policy form here.
    plan = retention.plan(db)

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
            "findings_job": findings_job,
            "report": report,
            "severities": SEVERITIES,
            "rules_off": rules_off,
            "rule_total": len(RULES),
            "plan": plan,
            "recent_jobs": list_jobs(db, limit=RECENT_JOBS),
            "human_bytes": human_bytes,
        },
    )
