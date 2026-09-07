"""The redaction preview: paste a log snippet, see what would be masked.

This exists so that what redaction does is visible *before* it is trusted with
a real bundle. Nothing here is stored — the pasted text lives only for the
length of the request — because the whole point of the page is that people try
it with text they would rather not leave behind.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, templates
from app.redact import RULES, redact_text

router = APIRouter()

# Enough to paste a handful of log lines into without the request becoming a
# way to hand the server arbitrary work. The preview is for a snippet; a whole
# file is what collection is for.
MAX_PREVIEW_CHARS = 20000

SAMPLE = """\
Sep  6 12:30:45 xen01.pool1.internal xapi: [debug] session.login_with_password \
trackid=a3f9c2b18e4d0c67 user=admin@pool1.internal
Sep  6 12:30:46 xen01.pool1.internal xapi: host 4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34 \
management address 10.20.30.41 netmask 255.255.255.0
Sep  6 12:30:47 xen01.pool1.internal xenopsd: VIF 3a:4b:5c:6d:7e:8f attached on 10.20.30.55
Sep  6 12:30:48 xen01.pool1.internal SM: mounting nfs://10.20.30.9/export password=Str0ngPass!
Sep  6 12:30:49 xen01.pool1.internal xapi: peer fe80::1c2d:3e4f:5a6b:7c8d replied in 12ms
Sep  6 12:30:50 xen01.pool1.internal xapi: health check from 127.0.0.1 ok
"""


@router.get("/redaction", response_class=HTMLResponse)
def redaction_page(request: Request, username: str = Depends(login_required)) -> Response:
    """The preview page, with a sample already in the box.

    Pre-filling it means the page answers "what does this do?" on the first
    load, without needing a real log to hand.
    """
    return _render(request, username, text=SAMPLE, result=None, counts={})


@router.post("/redaction", response_class=HTMLResponse)
def redaction_preview(
    request: Request,
    username: str = Depends(login_required),
    text: str = Form(""),
) -> Response:
    """Redact the pasted text and show both versions side by side."""
    error = None
    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS]
        error = f"Only the first {MAX_PREVIEW_CHARS:,} characters were previewed."

    result, counts = redact_text(text)
    return _render(request, username, text=text, result=result, counts=counts, error=error)


def _render(
    request: Request,
    username: str,
    *,
    text: str,
    result: str | None,
    counts: dict[str, int],
    error: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "redaction.html",
        {
            "username": username,
            "rules": RULES,
            "text": text,
            "result": result,
            "counts": counts,
            "total_hits": sum(counts.values()),
            "error": error,
        },
    )
