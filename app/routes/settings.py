"""The Xen Orchestra connection settings page."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, redirect, templates
from app.xo_client import XoClient, XoError
from app.xo_connection import (
    ACCOUNT_TYPES,
    DecryptionError,
    build_client,
    delete_connection,
    get_connection,
    record_test_result,
    save_connection,
)

router = APIRouter()
log = logging.getLogger("xcp_pulse.settings")


def _render(
    request: Request,
    username: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    test: object | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "username": username,
            "connection": get_connection(request.app.state.db),
            "account_types": ACCOUNT_TYPES,
            "error": error,
            "notice": notice,
            "test": test,
        },
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, username: str = Depends(login_required)) -> Response:
    return _render(request, username)


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    username: str = Depends(login_required),
    url: str = Form(...),
    token: str = Form(...),
    account_type: str = Form("admin"),
    verify_tls: str = Form(""),
) -> Response:
    url = url.strip()
    token = token.strip()

    if not url.startswith(("http://", "https://")):
        return _render(
            request,
            username,
            error="The address must start with http:// or https://.",
            status_code=400,
        )
    if not token:
        return _render(request, username, error="An API token is required.", status_code=400)
    if account_type not in ACCOUNT_TYPES:
        return _render(request, username, error="Unknown account type.", status_code=400)

    save_connection(
        request.app.state.db,
        url=url,
        token=token,
        account_type=account_type,
        verify_tls=bool(verify_tls),
        secret_key=request.app.state.settings.secret_key,
    )
    log.info("Xen Orchestra connection saved for %s", url)
    return redirect("/settings?saved=1")


@router.post("/settings/test", response_class=HTMLResponse)
def settings_test(request: Request, username: str = Depends(login_required)) -> Response:
    """Test the stored connection and report what the account can reach."""
    conn = request.app.state.db
    try:
        client: XoClient = build_client(conn, request.app.state.settings.secret_key)
    except LookupError:
        return _render(
            request,
            username,
            error="Save a connection before testing it.",
            status_code=400,
        )
    except DecryptionError:
        return _render(
            request,
            username,
            error=(
                "The stored token cannot be decrypted — the secret key has "
                "changed since it was saved. Enter the token again."
            ),
            status_code=400,
        )

    try:
        result = client.test_connection()
    except XoError as exc:
        record_test_result(conn, ok=False, message=str(exc))
        return _render(request, username, error=str(exc), status_code=502)

    record_test_result(conn, ok=result.ok, message=result.message)
    return _render(request, username, test=result)


@router.post("/settings/delete")
def settings_delete(request: Request, username: str = Depends(login_required)) -> Response:
    """Forget the connection, including the stored token."""
    delete_connection(request.app.state.db)
    log.info("Xen Orchestra connection deleted")
    return redirect("/settings?deleted=1")
