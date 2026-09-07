"""The landing page after logging in.

Shows the pools and hosts the stored Xen Orchestra connection can see. The
inventory is read live on each request rather than cached: these routes answer
in milliseconds, and a cached inventory would be one more thing to explain
being stale. The background job system arriving later is what changes that.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, templates
from app.xo_client import Inventory, XoError
from app.xo_connection import DecryptionError, build_client, get_connection

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


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, username: str = Depends(login_required)) -> Response:
    connection = get_connection(request.app.state.db)
    inventory: Inventory | None = None
    error: str | None = None

    if connection is not None:
        try:
            client = build_client(request.app.state.db, request.app.state.settings.secret_key)
            inventory = client.inventory()
        except DecryptionError:
            error = (
                "The stored token cannot be decrypted — the secret key has "
                "changed since it was saved. Enter the token again in Settings."
            )
        except LookupError:
            # The connection was deleted between the two reads above.
            connection = None
        except XoError as exc:
            error = str(exc)
            log.warning("inventory refresh failed: %s", exc)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "username": username,
            "connection": connection,
            "inventory": inventory,
            "error": error,
            "empty_help": EMPTY_HELP,
        },
    )
