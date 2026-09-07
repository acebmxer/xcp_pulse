"""The redaction preview: paste a log snippet, see what would be masked.

This exists so that what redaction does is visible *before* it is trusted with
a real bundle. Nothing pasted here is stored — the text lives only for the
length of the request — because the whole point of the page is that people try
it with text they would rather not leave behind. Which rules are switched on is
a different thing entirely: that is a setting, and it is stored.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, redirect, templates
from app.redact import RULES, enabled_rules, redact_text, set_enabled_rules

router = APIRouter()
log = logging.getLogger("xcp_pulse.redaction")

# Enough to paste a handful of log lines into without the request becoming a
# way to hand the server arbitrary work. The preview is for a snippet; a whole
# file is what collection is for.
MAX_PREVIEW_CHARS = 20000

# A module-level singleton because an unticked checkbox posts nothing at all, so
# the field must have a default — and a call in an argument default would be a
# mutable shared between requests.
_RULE_FIELD = Form(default_factory=list)

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
    """Redact the pasted text and show both versions side by side.

    The preview uses the stored rule settings rather than all of them, so what
    it shows is what a collected bundle would actually get.
    """
    error = None
    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS]
        error = f"Only the first {MAX_PREVIEW_CHARS:,} characters were previewed."

    enabled = enabled_rules(request.app.state.db)
    result, counts = redact_text(text, enabled)
    return _render(request, username, text=text, result=result, counts=counts, error=error)


@router.post("/redaction/rules")
def redaction_rules_save(
    request: Request,
    username: str = Depends(login_required),
    rule: list[str] = _RULE_FIELD,
) -> Response:
    """Store which rules are on.

    An unticked checkbox posts nothing, so the form's whole meaning is in which
    names arrive — which is why this writes the entire set rather than toggling
    one rule.
    """
    enabled = set_enabled_rules(request.app.state.db, rule)
    off = [r.name for r in RULES if r.name not in enabled]
    log.info("Redaction rules saved; off: %s", ", ".join(off) or "none")
    return redirect("/redaction?saved=1")


def _render(
    request: Request,
    username: str,
    *,
    text: str,
    result: str | None,
    counts: dict[str, int],
    error: str | None = None,
) -> Response:
    enabled = enabled_rules(request.app.state.db)
    return templates.TemplateResponse(
        request,
        "redaction.html",
        {
            "username": username,
            "rules": RULES,
            "enabled": enabled,
            "disabled_count": len(RULES) - len(enabled),
            "text": text,
            "result": result,
            "counts": counts,
            "total_hits": sum(counts.values()),
            "error": error,
        },
    )
