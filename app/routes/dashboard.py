"""The landing page after logging in.

Deliberately empty rather than a hollow frame: a Xen Orchestra connection can
now be configured under /settings, but listing its pools and hosts is the next
piece of work, so there is nothing true to show here yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, username: str = Depends(login_required)) -> Response:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"username": username},
    )
