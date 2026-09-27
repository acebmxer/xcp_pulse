"""The "Refresh inventory" job: read pools and hosts from XO, store the result.

This is what exercises the job system. It is deliberately a job whose endpoints
answer in milliseconds, so the queue, progress reporting, cancellation, failure
handling and the artifact store are all proven before the 100-second collection
job lands on them.

It replaces the read-on-page-load in the dashboard route. The difference that
matters is not speed: the inventory is now a *stored result with a time on it*,
so the page says when it was read, and a Xen Orchestra that is down leaves the
last known inventory on screen instead of an error where the hosts were.

A stored result would otherwise only ever change when an operator remembers to
click Refresh on the Jobs page, so a pool or host added or removed in Xen
Orchestra would sit unseen here indefinitely. ``ensure_fresh`` below is called
from several places that each catch a different way that can happen: on login
(catch drift since the last session), from Collect and Findings before they
build a host picker out of this (catch drift since the last visit to a page
that needs it), and periodically from the job worker's idle loop via
``app.job_runner.register_periodic`` (catch drift when nobody visits anything
at all). All four are one check, so "is it due" is answered the same way
everywhere rather than drifting between call sites; a manual Refresh from the
Jobs page still exists for "I want it now."
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from app.artifacts import list_for_job, read_json, store_json
from app.job_runner import register, register_periodic
from app.jobs import JobContext, enqueue, has_active, latest_job, latest_successful
from app.xo_client import Host, Inventory, Pool
from app.xo_connection import build_client, get_connection

KIND = "refresh_inventory"

# The artifact this job produces. One name, used to write it and to find it
# again, so the reader cannot drift from the writer.
INVENTORY_ARTIFACT = "inventory.json"

# How long a stored inventory is trusted before ``ensure_fresh`` queues a new
# read on its own. Pool and host topology does not change minute to minute, so
# this favours not hammering Xen Orchestra over catching a change instantly —
# an operator who wants it sooner still has the manual Refresh button.
STALE_AFTER_SECONDS = 15 * 60


def run(context: JobContext) -> None:
    """Read the inventory from Xen Orchestra and store it as an artifact.

    Raises rather than catching: the runner records the message against the job,
    which is where the operator will look for it. LookupError from
    ``build_client`` (no connection configured) and DecryptionError (the secret
    key changed) both carry their own explanation and are left to travel.
    """
    context.progress(10, "Connecting to Xen Orchestra")
    client = build_client(context.conn, context.settings.secret_key)

    context.progress(30, "Reading pools and hosts")
    inventory = client.inventory()

    context.progress(80, "Storing the result")
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=INVENTORY_ARTIFACT,
        payload=_to_payload(inventory),
    )
    context.progress(100, f"{len(inventory.pools)} pool(s), {len(inventory.hosts)} host(s)")


def _to_payload(inventory: Inventory) -> dict[str, Any]:
    """The inventory as plain JSON.

    ``asdict`` rather than a hand-written mapping so a field added to Pool or
    Host is stored without this being edited — the reader below fills anything
    missing from an older artifact with the dataclass default.
    """
    return {
        "pools": [asdict(pool) for pool in inventory.pools],
        "hosts": [asdict(host) for host in inventory.hosts],
    }


def inventory_from_job(conn, data_dir, job_id: str) -> Inventory | None:
    """Rebuild the Inventory a completed job stored, or None if it has none.

    Unknown keys are dropped and missing ones left at their default, so an
    artifact written by an older version still loads rather than raising on a
    field that has since changed.
    """
    artifact = next(
        (item for item in list_for_job(conn, job_id) if item.name == INVENTORY_ARTIFACT),
        None,
    )
    if artifact is None:
        return None

    try:
        payload = read_json(data_dir, artifact)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    return Inventory(
        pools=[_build(Pool, record) for record in _records(payload.get("pools"))],
        hosts=[_build(Host, record) for record in _records(payload.get("hosts"))],
    )


def known_inventory(conn, data_dir) -> Inventory:
    """The pools and hosts the last successful refresh stored.

    Read from the stored artifact rather than by calling Xen Orchestra: a page
    or job that only needs "what does the operator already know about the
    pool" must not depend on XO being reachable, and the host list shown is
    the one the operator already saw on the dashboard. The Collect page, a
    findings run and the Support Package page all want exactly this, so it is
    the one place any of them read it — do not add a second copy.
    """
    job = latest_successful(conn, KIND)
    if job is None:
        return Inventory()
    return inventory_from_job(conn, data_dir, job.id) or Inventory()


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _build(cls, record: dict[str, Any]):
    fields = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{key: value for key, value in record.items() if key in fields})


def ensure_fresh(conn) -> None:
    """Queue a refresh if none has ever run, or the newest is stale — unless
    one is already queued or running. A no-op with no connection configured.

    See the module docstring for the four places this is called from. This
    does not wait for the job it queues; callers that render a host list right
    after calling this may still show the previous result on this exact
    request, with the fresh one ready by the next one.
    """
    if get_connection(conn) is None:
        return
    if has_active(conn, KIND):
        return
    newest = latest_job(conn, KIND)
    if newest is not None and time.time() - newest.created_at < STALE_AFTER_SECONDS:
        return
    enqueue(conn, KIND)


register(KIND, run)
register_periodic(ensure_fresh)
