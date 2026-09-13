"""The in-app User manual: how to use XCP Pulse, rendered and searchable.

Covers the user guide plus Installation, Configuration and Architecture —
see app/docs_render.py for which files these are and why README.md and
docs/functions.md are deliberately not among them. Docs ship inside the
image (see the Dockerfile), so what renders here is exactly what shipped
with this build, not whatever is newest on GitHub.

Served under /help rather than /docs: FastAPI reserves /docs for its own
auto-generated API documentation, which this app deliberately disables (it
sits in front of credentials, and that page is unauthenticated) — a route
here at the same path would silently un-disable it in effect, by making
/docs answer with something instead of the 404 the app is tested to return.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, templates
from app.docs_render import DocPage, all_pages, get_page, search, sidebar_groups

router = APIRouter()


@router.get("/help", response_class=HTMLResponse)
def docs_index(request: Request, username: str = Depends(login_required)) -> Response:
    """The first page in sidebar order, so /help is never an empty landing page."""
    first_page = all_pages()[0]
    return _render(request, username, first_page, error=None)


@router.get("/help/search", response_class=HTMLResponse)
def docs_search(request: Request, q: str = "", username: str = Depends(login_required)) -> Response:
    results = search(q)
    return templates.TemplateResponse(
        request,
        "docs.html",
        {
            "username": username,
            "groups": sidebar_groups(),
            "active_slug": None,
            "page": None,
            "query": q,
            "results": results,
            "error": None,
        },
    )


@router.get("/help/{slug}", response_class=HTMLResponse)
def docs_page(request: Request, slug: str, username: str = Depends(login_required)) -> Response:
    page = get_page(slug)
    if page is None:
        first_page = all_pages()[0]
        return _render(request, username, first_page, error="That page does not exist.")
    return _render(request, username, page, error=None)


def _render(
    request: Request,
    username: str,
    page: DocPage,
    *,
    error: str | None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "docs.html",
        {
            "username": username,
            "groups": sidebar_groups(),
            "active_slug": page.slug,
            "page": page,
            "query": "",
            "results": None,
            "error": error,
        },
    )
