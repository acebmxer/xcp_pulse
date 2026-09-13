"""User management (admin-only) and self-service password changes (everyone).

Two different permission shapes live in one file because they're the same
underlying data (the users table) viewed from two directions: an admin
managing every account, and any signed-in user managing just their own
password. Splitting them into separate files would mean splitting app/users.py
imports and the activity-log calls across both for no real gain.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.activity import list_activity, log_activity
from app.dependencies import admin_required, login_required, operator_required, redirect, templates
from app.security import client_ip, verify_password
from app.totp import provisioning_uri, qr_svg
from app.users import (
    MIN_PASSWORD_LENGTH,
    ROLES,
    User,
    UserError,
    begin_totp_enrollment,
    confirm_totp_enrollment,
    create_user,
    disable_totp,
    get_user,
    get_user_by_id,
    list_users,
    set_disabled,
    set_password,
    set_role,
)

router = APIRouter()
log = logging.getLogger("xcp_pulse.users")


# --- Admin: manage every account ------------------------------------------------


def _render_users(
    request: Request, username: str, *, error: str | None = None, notice: str | None = None
) -> Response:
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "username": username,
            "users": list_users(request.app.state.db),
            "roles": ROLES,
            "error": error,
            "notice": notice,
        },
    )


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(request: Request, username: str = Depends(admin_required)) -> Response:
    return _render_users(request, username)


@router.post("/settings/users", response_class=HTMLResponse)
def users_create(
    request: Request,
    username: str = Depends(admin_required),
    new_username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
) -> Response:
    conn = request.app.state.db
    if password != confirm_password:
        return _render_users(request, username, error="Password and confirmation do not match.")
    try:
        created = create_user(conn, new_username, password, role)
    except UserError as exc:
        return _render_users(request, username, error=str(exc))

    log_activity(
        conn,
        username,
        "user.create",
        detail=f"{created.username} ({created.role})",
        ip=client_ip(request),
    )
    return redirect("/settings/users?created=1")


@router.post("/settings/users/{user_id}/role")
def users_set_role(
    request: Request,
    user_id: str,
    username: str = Depends(admin_required),
    role: str = Form(...),
) -> Response:
    conn = request.app.state.db
    target = get_user_by_id(conn, user_id)
    try:
        set_role(conn, user_id, role)
    except UserError as exc:
        return _render_users(request, username, error=str(exc))

    if target is not None:
        log_activity(
            conn,
            username,
            "user.role",
            detail=f"{target.username} -> {role}",
            ip=client_ip(request),
        )
    return redirect("/settings/users?updated=1")


@router.post("/settings/users/{user_id}/disable")
def users_disable(
    request: Request, user_id: str, username: str = Depends(admin_required)
) -> Response:
    conn = request.app.state.db
    target = get_user_by_id(conn, user_id)
    try:
        set_disabled(conn, user_id, True)
    except UserError as exc:
        return _render_users(request, username, error=str(exc))

    if target is not None:
        log_activity(conn, username, "user.disable", detail=target.username, ip=client_ip(request))
    return redirect("/settings/users?updated=1")


@router.post("/settings/users/{user_id}/enable")
def users_enable(
    request: Request, user_id: str, username: str = Depends(admin_required)
) -> Response:
    conn = request.app.state.db
    target = get_user_by_id(conn, user_id)
    set_disabled(conn, user_id, False)
    if target is not None:
        log_activity(conn, username, "user.enable", detail=target.username, ip=client_ip(request))
    return redirect("/settings/users?updated=1")


@router.post("/settings/users/{user_id}/disable-totp")
def users_disable_totp(
    request: Request, user_id: str, username: str = Depends(admin_required)
) -> Response:
    """An admin turns off someone else's two-factor authentication, no code
    or password check — the recovery path for a user who has lost both their
    authenticator device and all ten backup codes. Distinct from
    /account/totp/disable, which is self-service and does require the
    current password.
    """
    conn = request.app.state.db
    target = get_user_by_id(conn, user_id)
    if target is None:
        return _render_users(request, username, error="That user no longer exists.")

    disable_totp(conn, target.id)
    log_activity(conn, username, "user.totp_disable", detail=target.username, ip=client_ip(request))
    return redirect("/settings/users?updated=1")


@router.post("/settings/users/{user_id}/reset-password", response_class=HTMLResponse)
def users_reset_password(
    request: Request,
    user_id: str,
    username: str = Depends(admin_required),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    """An admin sets someone else's password directly, no current-password
    check — that's the whole point of a reset (the user has presumably lost
    it). Distinct from /account/password below, which is self-service and
    does require the current password.
    """
    conn = request.app.state.db
    target = get_user_by_id(conn, user_id)
    if target is None:
        return _render_users(request, username, error="That user no longer exists.")
    if new_password != confirm_password:
        return _render_users(request, username, error="Password and confirmation do not match.")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return _render_users(
            request, username, error=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )

    set_password(conn, user_id, new_password)
    log_activity(
        conn, username, "user.reset_password", detail=target.username, ip=client_ip(request)
    )
    return redirect("/settings/users?reset=1")


# --- Self-service: change password and manage 2FA, one combined page ----------


def _render_account(
    request: Request, username: str, error: str | None, status_code: int = 200
) -> Response:
    user = get_user(request.app.state.db, username)
    return templates.TemplateResponse(
        request,
        "account.html",
        {"username": username, "user": user, "error": error},
        status_code=status_code,
    )


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, username: str = Depends(login_required)) -> Response:
    return _render_account(request, username, error=None)


@router.post("/account/password", response_class=HTMLResponse)
def account_password_change(
    request: Request,
    username: str = Depends(login_required),
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    conn = request.app.state.db
    user: User | None = get_user(conn, username)
    if user is None or not verify_password(current_password, user.password_hash):
        return _render_account(request, username, "Current password is incorrect.", 401)
    if new_password != confirm_password:
        return _render_account(
            request, username, "New password and confirmation do not match.", 400
        )
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return _render_account(
            request,
            username,
            f"New password must be at least {MIN_PASSWORD_LENGTH} characters.",
            400,
        )

    set_password(conn, user.id, new_password)
    log_activity(conn, username, "self.password_change", ip=client_ip(request))
    return redirect("/account?changed=1")


# --- Self-service: optional TOTP two-factor ------------------------------------


@router.get("/account/totp/setup", response_class=HTMLResponse)
def account_totp_setup(request: Request, username: str = Depends(login_required)) -> Response:
    """Generate (or regenerate) a pending secret and show its QR code.

    Safe to reach repeatedly — see begin_totp_enrollment's docstring — so a
    user who navigates away mid-setup and comes back just gets a fresh code
    and a fresh QR image rather than an error.
    """
    conn = request.app.state.db
    settings = request.app.state.settings
    user = get_user(conn, username)
    if user is None:
        return redirect("/account")
    secret = begin_totp_enrollment(conn, user.id, settings.secret_key)
    uri = provisioning_uri(secret, username=username)
    return templates.TemplateResponse(
        request,
        "account_totp_setup.html",
        {"username": username, "secret": secret, "qr_svg": qr_svg(uri), "error": None},
    )


@router.post("/account/totp/setup", response_class=HTMLResponse)
def account_totp_confirm(
    request: Request,
    username: str = Depends(login_required),
    code: str = Form(...),
) -> Response:
    conn = request.app.state.db
    settings = request.app.state.settings
    user = get_user(conn, username)
    if user is None:
        return redirect("/account")

    backup_codes = confirm_totp_enrollment(conn, user.id, code, settings.secret_key)
    if backup_codes is None:
        # Wrong code — start setup over with a fresh secret and QR code
        # rather than showing a blank form: the secret the user just typed a
        # code for might have been mistyped or the QR misread, and there is
        # no way to tell which from here, so the safest recovery is a clean
        # new attempt.
        secret = begin_totp_enrollment(conn, user.id, settings.secret_key)
        uri = provisioning_uri(secret, username=username)
        return templates.TemplateResponse(
            request,
            "account_totp_setup.html",
            {
                "username": username,
                "secret": secret,
                "qr_svg": qr_svg(uri),
                "error": "Incorrect code. Scan the new QR code below and try again.",
            },
            status_code=400,
        )

    log_activity(conn, username, "self.totp_enable", ip=client_ip(request))
    return templates.TemplateResponse(
        request,
        "account_totp_backup_codes.html",
        {"username": username, "backup_codes": backup_codes},
    )


@router.post("/account/totp/disable", response_class=HTMLResponse)
def account_totp_disable(
    request: Request,
    username: str = Depends(login_required),
    current_password: str = Form(...),
) -> Response:
    """Turning 2FA off requires the current password — the same bar as
    changing it — since disabling it is a step down in how well an account
    is protected and should not be a single unauthenticated-adjacent click.
    """
    conn = request.app.state.db
    user = get_user(conn, username)
    if user is None or not verify_password(current_password, user.password_hash):
        return _render_account(request, username, "Current password is incorrect.", 401)
    disable_totp(conn, user.id)
    log_activity(conn, username, "self.totp_disable", ip=client_ip(request))
    return redirect("/account?disabled=1")


# --- Activity log: admin and operator -------------------------------------------


@router.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, username: str = Depends(operator_required)) -> Response:
    return templates.TemplateResponse(
        request,
        "activity.html",
        {"username": username, "entries": list_activity(request.app.state.db)},
    )
