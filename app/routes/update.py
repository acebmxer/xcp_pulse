"""The Update page: is a newer version out, and applying it.

A standalone page rather than a card on Settings, because Settings is
admin-only (Xen Orchestra credentials, TLS certificates, user management) and
keeping the app itself current is something operators do too — the same
split as running a collection or a redaction from the Jobs page.

No JSON API here: this app has no client-side JavaScript anywhere (see
app/templates/jobs.html's meta-refresh polling for how it handles a
long-running operation instead), so this is a page with plain forms.

Every route is a no-op when self-update is disabled, on top of the page
itself explaining why there is nothing to click — see app/update.py's module
docstring for why applying an update needs the Docker socket, which this
project does not grant by default.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app import __version__
from app.activity import log_activity
from app.dependencies import operator_required, redirect, templates
from app.security import client_ip
from app.update import (
    check_for_updates,
    clear_result,
    current_state,
    reap_stalled_update,
    run_update,
)

router = APIRouter()
log = logging.getLogger("xcp_pulse.update")


@router.get("/update", response_class=HTMLResponse)
def update_page(request: Request, username: str = Depends(operator_required)) -> Response:
    settings = request.app.state.settings
    db = request.app.state.db

    state = None
    if settings.enable_self_update:
        reap_stalled_update(db)
        state = current_state(db)

    return templates.TemplateResponse(
        request,
        "update.html",
        {
            "username": username,
            "enabled": settings.enable_self_update,
            "is_dev_build": settings.is_dev_build,
            "version": __version__,
            "state": state,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "update_started": bool(request.query_params.get("update_started")),
        },
    )


@router.post("/update/check")
def check_now(request: Request, username: str = Depends(operator_required)) -> Response:
    settings = request.app.state.settings
    if not settings.enable_self_update:
        return redirect("/update")

    db = request.app.state.db
    reap_stalled_update(db)
    available = check_for_updates(db, is_dev_build=settings.is_dev_build)
    if available:
        return redirect("/update")
    return redirect("/update?notice=No+update+available.")


@router.post("/update/apply")
def apply_update(request: Request, username: str = Depends(operator_required)) -> Response:
    settings = request.app.state.settings
    if not settings.enable_self_update:
        return redirect("/update")

    db = request.app.state.db
    state = current_state(db)
    if state.in_progress:
        return redirect("/update?error=An+update+is+already+in+progress.")
    if not state.available:
        return redirect("/update?error=No+update+is+available.")

    digest = state.latest_digest[:12]
    log.info("update to %s started by %s", digest, username)
    log_activity(db, username, "update.apply", detail=digest, ip=client_ip(request))
    threading.Thread(
        target=run_update, args=(settings, settings.db_path), daemon=True, name="update-apply"
    ).start()
    return redirect("/update?update_started=1")


@router.get("/update/apply-anyway", response_class=HTMLResponse)
def apply_anyway_confirm(request: Request, username: str = Depends(operator_required)) -> Response:
    """A confirmation page in front of apply_anyway below.

    Added after this ran from a single, unprompted click on a real dev
    deployment and silently replaced the running dev build with the
    published :latest image — exactly what the button is documented to do,
    but with nothing standing between an idle click and losing whatever
    uncommitted work was running. Every other destructive action in this app
    is also one click (see jobs.html's Delete), so there is no existing
    confirm pattern to reuse; this is a plain second page rather than a JS
    confirm() dialog, since this app has no client-side JavaScript anywhere.
    """
    settings = request.app.state.settings
    if not settings.enable_self_update or not settings.is_dev_build:
        return redirect("/update")
    return templates.TemplateResponse(
        request,
        "update_confirm.html",
        {"username": username},
    )


@router.post("/update/apply-anyway")
def apply_anyway(request: Request, username: str = Depends(operator_required)) -> Response:
    """Run the real pull-and-recreate cycle on a dev build, ignoring whether
    anything is actually available.

    Only reachable on a dev build (see the template — the button doesn't
    render otherwise, checked again here rather than trusted from a hidden
    control). check_for_updates deliberately never reports "available" for a
    dev build (see its docstring: this image is likely *ahead* of :latest,
    not behind it, and a bare digest comparison can't tell those apart) — so
    without this, there would be no way to watch the actual mechanism run
    against a local build at all. It pulls the real published :latest and
    restarts to it, same as a genuine apply; it does not preserve whatever
    dev-branch code is running now. Only reachable via the confirmation page
    above now — see apply_anyway_confirm's docstring for why.
    """
    settings = request.app.state.settings
    if not settings.enable_self_update or not settings.is_dev_build:
        return redirect("/update")

    db = request.app.state.db
    if current_state(db).in_progress:
        return redirect("/update?error=An+update+is+already+in+progress.")

    log.info("dev-build test update started by %s", username)
    log_activity(db, username, "update.apply_anyway", ip=client_ip(request))
    threading.Thread(
        target=run_update, args=(settings, settings.db_path), daemon=True, name="update-apply"
    ).start()
    return redirect("/update?update_started=1")


@router.post("/update/dismiss")
def dismiss_result(request: Request, username: str = Depends(operator_required)) -> Response:
    settings = request.app.state.settings
    if settings.enable_self_update:
        clear_result(request.app.state.db)
    return redirect("/update")
