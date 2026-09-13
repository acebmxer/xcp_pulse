"""Login and logout."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.activity import log_activity
from app.dependencies import redirect, templates
from app.security import (
    PENDING_2FA_COOKIE,
    PENDING_2FA_MINUTES,
    SESSION_COOKIE,
    clear_login_failures,
    client_ip,
    create_session,
    current_user,
    destroy_session,
    is_rate_limited,
    record_login_failure,
    sign_pending_2fa,
    sign_session_id,
    unsign_session_id,
    verify_pending_2fa,
)
from app.users import AccountDisabled, authenticate, get_user, verify_totp_or_backup_code

router = APIRouter()


def _set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session_id(session_id, settings.secret_key),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.https_only,
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> Response:
    # Already signed in — no reason to show the form again.
    if current_user(request) is not None:
        return redirect(next or "/")
    return templates.TemplateResponse(request, "login.html", {"next_url": next})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    settings = request.app.state.settings
    conn = request.app.state.db
    ip = client_ip(request)

    if is_rate_limited(conn, ip, settings.login_max_attempts, settings.login_lockout_minutes):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": (
                    f"Too many failed attempts. Try again in "
                    f"{settings.login_lockout_minutes} minutes."
                ),
                "next_url": next,
            },
            status_code=429,
        )

    # authenticate() verifies the password even when the username doesn't
    # exist, so a bad username and a bad password take the same time and
    # cannot be told apart. A disabled account with the *correct* password
    # raises AccountDisabled instead — that case alone gets a specific
    # message, since only someone who already knows the password learns
    # anything from it.
    try:
        user = authenticate(conn, username, password)
    except AccountDisabled:
        record_login_failure(conn, ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "This account has been disabled.", "next_url": next},
            status_code=401,
        )
    if user is None:
        record_login_failure(conn, ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect username or password.", "next_url": next},
            status_code=401,
        )

    clear_login_failures(conn, ip)

    # Only ever redirect within this site: an attacker-supplied ?next=https://…
    # would otherwise turn the login page into an open redirect.
    target = next if next.startswith("/") and not next.startswith("//") else "/"

    if user.totp_enabled:
        # Password was right, but that alone must not create a session — the
        # second factor still has to be checked. A short-lived signed cookie
        # carries the username to /login/2fa; it proves only "the password
        # step just succeeded", not "this user is signed in" (see
        # PENDING_2FA_COOKIE's docstring in app/security.py).
        response = redirect(f"/login/2fa?next={target}")
        response.set_cookie(
            key=PENDING_2FA_COOKIE,
            value=sign_pending_2fa(user.username, settings.secret_key),
            max_age=PENDING_2FA_MINUTES * 60,
            httponly=True,
            samesite="lax",
            secure=settings.https_only,
            path="/login/2fa",
        )
        return response

    session_id = create_session(conn, user.username, settings.session_hours)
    log_activity(conn, user.username, "login", ip=ip)
    response = redirect(target)
    _set_session_cookie(response, request, session_id)
    return response


def _pending_username(request: Request) -> str | None:
    cookie = request.cookies.get(PENDING_2FA_COOKIE)
    if not cookie:
        return None
    return verify_pending_2fa(cookie, request.app.state.settings.secret_key)


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_form(request: Request, next: str = "/") -> Response:
    if current_user(request) is not None:
        return redirect(next or "/")
    if _pending_username(request) is None:
        # No password step completed recently (cookie missing, expired, or
        # tampered with) — send back to the start rather than showing a code
        # form with nothing behind it.
        return redirect("/login")
    return templates.TemplateResponse(request, "login_2fa.html", {"next_url": next})


@router.post("/login/2fa", response_class=HTMLResponse)
def login_2fa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form("/"),
) -> Response:
    settings = request.app.state.settings
    conn = request.app.state.db
    ip = client_ip(request)

    username = _pending_username(request)
    if username is None:
        return redirect("/login")

    if is_rate_limited(conn, ip, settings.login_max_attempts, settings.login_lockout_minutes):
        return templates.TemplateResponse(
            request,
            "login_2fa.html",
            {
                "error": (
                    f"Too many failed attempts. Try again in "
                    f"{settings.login_lockout_minutes} minutes."
                ),
                "next_url": next,
            },
            status_code=429,
        )

    user = get_user(conn, username)
    if user is None or not user.totp_enabled:
        # The account was disabled or had 2FA turned off between the
        # password step and this one — start over rather than trust a stale
        # pending cookie.
        return redirect("/login")

    if not verify_totp_or_backup_code(conn, user, code, settings.secret_key):
        record_login_failure(conn, ip)
        return templates.TemplateResponse(
            request,
            "login_2fa.html",
            {"error": "Incorrect code.", "next_url": next},
            status_code=401,
        )

    clear_login_failures(conn, ip)
    session_id = create_session(conn, user.username, settings.session_hours)
    log_activity(conn, user.username, "login", ip=ip)

    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = redirect(target)
    _set_session_cookie(response, request, session_id)
    response.delete_cookie(PENDING_2FA_COOKIE, path="/login/2fa")
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session_id = unsign_session_id(cookie, request.app.state.settings.secret_key)
        if session_id:
            conn = request.app.state.db
            username = current_user(request)
            destroy_session(conn, session_id)
            if username is not None:
                log_activity(conn, username, "logout", ip=client_ip(request))

    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
