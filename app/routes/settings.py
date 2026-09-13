"""The Xen Orchestra connection settings page, and the TLS certificate."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app.dependencies import login_required, redirect, templates
from app.tls import (
    CertificateError,
    current_certificate_info,
    install_certificate,
    validate_certificate_pair,
)
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

# A cert and a key together are a few KB; this is generous headroom rather
# than a tuned limit, just enough to reject someone pasting the wrong file
# (a multi-megabyte bundle export, say) with a clear message instead of
# reading it entirely first.
MAX_CERT_FILE_BYTES = 1_000_000

# Module-level singletons because a call in an argument default would be a
# mutable shared between requests — the same reason redaction.py's rule
# checkboxes use a module-level Form().
_CERT_FILE_FIELD = File(...)
_KEY_FILE_FIELD = File(...)


def _render(
    request: Request,
    username: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    test: object | None = None,
    tls_error: str | None = None,
    status_code: int = 200,
) -> Response:
    settings = request.app.state.settings
    current_cert = (
        current_certificate_info(settings.data_dir / "tls") if settings.enable_https else None
    )
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
            "enable_https": settings.enable_https,
            "current_cert": current_cert,
            "tls_error": tls_error,
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


@router.post("/settings/tls", response_class=HTMLResponse)
async def settings_tls_upload(
    request: Request,
    username: str = Depends(login_required),
    cert_file: UploadFile = _CERT_FILE_FIELD,
    key_file: UploadFile = _KEY_FILE_FIELD,
) -> Response:
    """Replace the certificate nginx serves with an operator-supplied pair.

    Only reachable when built-in HTTPS is on — the form itself is hidden
    otherwise, and there is no nginx to hand a certificate to. Checked again
    here rather than trusted from the hidden form, since a hidden control is
    not an access control.
    """
    if not request.app.state.settings.enable_https:
        return _render(
            request,
            username,
            tls_error="Built-in HTTPS is not enabled — nothing to upload a certificate to.",
            status_code=400,
        )

    cert_pem = await cert_file.read(MAX_CERT_FILE_BYTES + 1)
    key_pem = await key_file.read(MAX_CERT_FILE_BYTES + 1)
    if len(cert_pem) > MAX_CERT_FILE_BYTES or len(key_pem) > MAX_CERT_FILE_BYTES:
        return _render(
            request,
            username,
            tls_error="That file is too large to be a certificate or private key.",
            status_code=400,
        )

    try:
        validate_certificate_pair(cert_pem, key_pem)
    except CertificateError as exc:
        return _render(request, username, tls_error=str(exc), status_code=400)

    tls_dir = request.app.state.settings.data_dir / "tls"
    install_certificate(tls_dir, cert_pem, key_pem)
    log.info("TLS certificate replaced by %s", username)
    return redirect("/settings?tls_saved=1")
