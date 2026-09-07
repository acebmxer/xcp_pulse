"""The landing page after logging in.

Empty by design in v0.1.0 — there is no XO connection until v0.2.0, and the
page says so rather than showing a hollow frame.
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
